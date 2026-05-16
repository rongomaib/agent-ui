import asyncio
import json
import os
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import AsyncGenerator, Optional

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

REPO_PATH = Path(os.getenv("REPO_PATH", r"C:\Users\imdyi\OneDrive\Desktop\Claude\Focus-app"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "rongomaib/Focus-app")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
APP_PORT = int(os.getenv("APP_PORT", "3000"))

aclient = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AGENTS_FILE = Path(__file__).parent / "agents.json"
app_process: Optional[subprocess.Popen] = None
app_logs: deque = deque(maxlen=200)


def _pipe_reader(stream, label: str):
    try:
        for line in iter(stream.readline, b""):
            app_logs.append(f"[{label}] {line.decode('utf-8', errors='replace').rstrip()}")
    except Exception:
        pass


def _save_agents(agents: dict):
    data = {
        aid: {k: v for k, v in a.items() if k != "history"}
        for aid, a in agents.items()
    }
    AGENTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_agents() -> dict:
    if not AGENTS_FILE.exists():
        return {}
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
        for a in data.values():
            a.setdefault("messages", [])
            a["history"] = []  # history is not persisted; agent re-reads files as needed
        return data
    except Exception:
        return {}


agents: dict = _load_agents()

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the repository. Returns the file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the repository directory. Use for git, tests, linting, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories at a path in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to repo root. Defaults to root.",
                }
            },
            "required": [],
        },
    },
]


def run_git(args: list[str]) -> tuple[str, str]:
    result = subprocess.run(
        ["git"] + args,
        cwd=str(REPO_PATH),
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip(), result.stderr.strip()


def execute_tool(name: str, inputs: dict) -> str:
    try:
        if name == "read_file":
            path = REPO_PATH / inputs["path"]
            return path.read_text(encoding="utf-8", errors="replace")

        if name == "write_file":
            path = REPO_PATH / inputs["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inputs["content"], encoding="utf-8")
            return f"Wrote {inputs['path']}"

        if name == "run_command":
            result = subprocess.run(
                inputs["command"],
                shell=True,
                cwd=str(REPO_PATH),
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            out = result.stdout
            if result.stderr:
                out += f"\nSTDERR: {result.stderr}"
            return out or "(no output)"

        if name == "list_directory":
            path = REPO_PATH / inputs.get("path", ".")
            items = []
            for item in sorted(path.iterdir()):
                prefix = "[dir] " if item.is_dir() else "[file]"
                items.append(f"{prefix} {item.name}")
            return "\n".join(items) if items else "(empty)"

        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"


def block_to_dict(block) -> dict:
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": block.type}


# --- Pydantic models ---
class CreateAgentRequest(BaseModel):
    name: str
    branch: str = ""
    model: str = DEFAULT_MODEL


class MessageRequest(BaseModel):
    content: str


class PushRequest(BaseModel):
    branch: str


class BranchRequest(BaseModel):
    name: str


class CheckoutRequest(BaseModel):
    branch: str


class MergeRequest(BaseModel):
    branch: str
    base: str = "main"


class RevertRequest(BaseModel):
    commit: str


class PRRequest(BaseModel):
    title: str
    body: str = ""
    head: str
    base: str = "main"


# --- Git routes ---
@app.get("/api/git/status")
def git_status():
    status, _ = run_git(["status", "--short"])
    branch, _ = run_git(["branch", "--show-current"])
    log, _ = run_git(["log", "--oneline", "-5"])
    return {"status": status, "branch": branch, "log": log}


@app.get("/api/git/branches")
def git_branches():
    stdout, _ = run_git(["branch", "-a"])
    branches = []
    for b in stdout.splitlines():
        name = b.strip().lstrip("* ").strip()
        if name and "HEAD" not in name:
            branches.append(name)
    return {"branches": branches}


@app.post("/api/git/pull")
def git_pull():
    branch, _ = run_git(["branch", "--show-current"])
    stdout, stderr = run_git(["pull", "origin", branch])
    return {"output": stdout or stderr}


@app.post("/api/git/push")
def git_push(req: PushRequest):
    stdout, stderr = run_git(["push", "-u", "origin", req.branch])
    return {"output": stdout or stderr}


@app.post("/api/git/branch")
def git_create_branch(req: BranchRequest):
    stdout, stderr = run_git(["checkout", "-b", req.name])
    return {"output": stdout or stderr, "branch": req.name}


@app.post("/api/git/checkout")
def git_checkout(req: CheckoutRequest):
    stdout, stderr = run_git(["checkout", req.branch])
    return {"output": stdout or stderr}


@app.post("/api/git/merge-to-main")
def merge_to_main(req: MergeRequest):
    steps = []
    for args in [
        ["checkout", req.base],
        ["pull", "origin", req.base],
        ["merge", "--no-ff", req.branch, "-m", f"Merge {req.branch} into {req.base}"],
        ["push", "origin", req.base],
        ["checkout", req.branch],
    ]:
        out, err = run_git(args)
        steps.append(out or err)
        if err and any(w in err.lower() for w in ["conflict", "error", "fatal"]):
            run_git(["checkout", req.branch])
            return {"ok": False, "error": err, "steps": steps}
    return {"ok": True, "steps": steps}


@app.get("/api/git/log")
def git_log():
    stdout, _ = run_git(["log", "main", "--oneline", "-20", "--no-walk=unsorted"])
    commits = []
    for line in stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append({"hash": parts[0], "message": parts[1]})
    return {"commits": commits}


@app.post("/api/git/revert")
def git_revert(req: RevertRequest):
    out, err = run_git(["revert", "--no-edit", req.commit])
    if err and "error" in err.lower():
        return {"ok": False, "error": err}
    push_out, push_err = run_git(["push", "origin", "main"])
    return {"ok": True, "output": out or err, "push": push_out or push_err}


# --- GitHub PR ---
@app.post("/api/github/pr")
async def create_pr(req: PRRequest):
    async with httpx.AsyncClient() as hclient:
        resp = await hclient.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/pulls",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": req.title, "body": req.body, "head": req.head, "base": req.base},
            timeout=15,
        )
    data = resp.json()
    if "html_url" in data:
        return {"url": data["html_url"], "number": data["number"]}
    return {"error": data.get("message", "Unknown error"), "details": data.get("errors")}


# --- App server management ---
@app.post("/api/app/start")
async def start_app():
    global app_process
    if app_process and app_process.poll() is None:
        return {"status": "already_running", "pid": app_process.pid}

    app_logs.clear()
    app_logs.append(f"[studio] Working directory: {REPO_PATH}")

    # Auto-install node_modules if missing
    if not (REPO_PATH / "node_modules").exists():
        app_logs.append("[studio] node_modules not found — running npm install first…")
        try:
            install = subprocess.Popen(
                "npm install",
                cwd=str(REPO_PATH),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
            )
            threading.Thread(target=_pipe_reader, args=(install.stdout, "install"), daemon=True).start()
            threading.Thread(target=_pipe_reader, args=(install.stderr, "install"), daemon=True).start()
            await asyncio.to_thread(install.wait)
            if install.returncode != 0:
                app_logs.append(f"[studio] ERROR: npm install failed (exit {install.returncode})")
                return {"status": "error", "error": "npm install failed"}
            app_logs.append("[studio] npm install complete — starting dev server…")
        except FileNotFoundError:
            app_logs.append("[studio] ERROR: 'npm' not found — is Node.js installed and on PATH?")
            return {"status": "error", "error": "npm not found"}
    else:
        app_logs.append("[studio] Starting Focus-app with: npm run dev")

    try:
        app_process = subprocess.Popen(
            "npm run dev",
            cwd=str(REPO_PATH),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
        )
    except FileNotFoundError:
        app_logs.append("[studio] ERROR: 'npm' not found — is Node.js installed and on PATH?")
        return {"status": "error", "error": "npm not found"}

    threading.Thread(target=_pipe_reader, args=(app_process.stdout, "stdout"), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(app_process.stderr, "stderr"), daemon=True).start()

    await asyncio.sleep(1)
    return {"status": "started", "pid": app_process.pid}


@app.post("/api/app/stop")
async def stop_app():
    global app_process
    if app_process:
        app_logs.append("[studio] Stopping app…")
        app_process.terminate()
        app_process = None
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.get("/api/app/status")
def app_status():
    running = app_process is not None and app_process.poll() is None
    exit_code = None
    if app_process and app_process.poll() is not None:
        exit_code = app_process.poll()
    return {"running": running, "pid": app_process.pid if running else None, "port": APP_PORT, "exit_code": exit_code}


@app.get("/api/app/logs")
def get_app_logs():
    running = app_process is not None and app_process.poll() is None
    exit_code = app_process.poll() if app_process else None
    return {"logs": list(app_logs), "running": running, "exit_code": exit_code}


# --- Agent routes ---
@app.get("/api/agents")
def list_agents():
    return {
        "agents": [
            {k: v for k, v in a.items() if k != "history"}
            for a in agents.values()
        ]
    }


@app.post("/api/agents")
def create_agent(req: CreateAgentRequest):
    agent_id = str(uuid.uuid4())[:8]
    branch = req.branch or f"agent/{req.name.lower().replace(' ', '-')}"

    out, err = run_git(["checkout", "-b", branch])
    if "already exists" in err:
        run_git(["checkout", branch])

    agents[agent_id] = {
        "id": agent_id,
        "name": req.name,
        "branch": branch,
        "model": req.model,
        "history": [],
        "messages": [],
    }
    _save_agents(agents)
    return {k: v for k, v in agents[agent_id].items() if k != "history"}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    if agent_id not in agents:
        raise HTTPException(404, "Agent not found")
    del agents[agent_id]
    _save_agents(agents)
    return {"ok": True}


@app.post("/api/agents/{agent_id}/message")
async def send_message(agent_id: str, req: MessageRequest):
    if agent_id not in agents:
        raise HTTPException(404, "Agent not found")

    agent = agents[agent_id]

    async def generate() -> AsyncGenerator[str, None]:
        # Record user message for display
        agent["messages"].append({"role": "user", "content": req.content, "type": "text"})
        _save_agents(agents)

        # Keep last 10 messages to stay within token limits
        history = list(agent["history"])[-10:]
        history.append({"role": "user", "content": req.content})

        system = (
            f"You are a coding assistant working on the Focus-app project. "
            f"You are on git branch '{agent['branch']}'. "
            f"The repository is at {REPO_PATH}. "
            f"Use tools to read, write, and modify files. "
            f"Match the existing code style. Think step by step before making changes. "
            f"IMPORTANT: Your conversation context is limited to the last 10 messages. "
            f"You may not have access to earlier parts of this conversation. "
            f"If you're missing context, use your tools to re-read the relevant files rather than relying on memory."
        )

        while True:
            stop_reason = None
            response_blocks = []

            try:
                async with aclient.messages.stream(
                    model=agent["model"],
                    max_tokens=8096,
                    system=system,
                    messages=history,
                    tools=TOOLS,
                ) as stream:
                    assistant_text = ""
                    async for event in stream:
                        etype = getattr(event, "type", None)

                        if etype == "content_block_start":
                            cb = getattr(event, "content_block", None)
                            if cb and cb.type == "tool_use":
                                agent["messages"].append({"role": "tool", "content": f"Running {cb.name}…", "type": "tool_start"})
                                yield f"data: {json.dumps({'type': 'tool_start', 'name': cb.name})}\n\n"

                        elif etype == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if delta and hasattr(delta, "text") and delta.text:
                                assistant_text += delta.text
                                yield f"data: {json.dumps({'type': 'text', 'text': delta.text})}\n\n"

                    final = await stream.get_final_message()
                    stop_reason = final.stop_reason
                    response_blocks = final.content
                    if assistant_text:
                        agent["messages"].append({"role": "assistant", "content": assistant_text, "type": "text"})

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                break

            history.append({"role": "assistant", "content": [block_to_dict(b) for b in response_blocks]})

            if stop_reason != "tool_use":
                break

            tool_results = []
            for block in response_blocks:
                if block.type == "tool_use":
                    result = await asyncio.to_thread(execute_tool, block.name, block.input)
                    preview = result[:400] + "..." if len(result) > 400 else result
                    agent["messages"].append({"role": "tool", "content": f"{block.name} → {preview}", "type": "tool_result"})
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': block.name, 'result': preview})}\n\n"
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            history.append({"role": "user", "content": tool_results})

        agent["history"] = history
        _save_agents(agents)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# Serve frontend — must be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
