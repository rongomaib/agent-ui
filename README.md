# Agent Studio

A local web UI for running multiple Claude Code agents in parallel on any git repository. Each agent works on its own branch, can read and modify files, run commands, and push changes — all visible in real time.

![Agent Studio](https://img.shields.io/badge/powered%20by-Claude%20Code-6366f1)

## What it does

- **Multi-agent workspace** — spin up several Claude agents simultaneously, each on its own git branch
- **Live chat** — send tasks, paste screenshots, and watch agents stream their responses and tool calls
- **Built-in git** — pull, push, branch switch, merge to main, create PRs, and revert commits from the UI
- **App preview** — start your project's dev server and preview it (desktop or mobile frame) without leaving the tab
- **Any repo** — configure it to point at any local git repository via the settings panel

## Prerequisites

- [Python 3.11+](https://python.org)
- [Node.js](https://nodejs.org) (only needed if your project uses npm)
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on your PATH)
- A GitHub personal access token (for push/PR features)

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/rongomaib/agent-ui.git
cd agent-ui

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Copy the example settings
cp settings.example.json settings.json

# 4. Start the server
python main.py
```

Then open **http://localhost:8000** in your browser.

On first launch the Settings panel opens automatically — fill in your repo path and other details to get started.

## Configuration

All configuration lives in `settings.json` (gitignored — never committed):

| Field | Description |
|---|---|
| `repo_path` | Absolute path to your local git repository |
| `github_repo` | GitHub repo in `owner/repo` format (for PRs) |
| `github_token` | GitHub personal access token (needs `repo` scope) |
| `start_command` | Command to start your dev server (e.g. `npm run dev`) |
| `app_port` | Port your dev server runs on (default `3000`) |

You can also edit these at any time via the **⚙ Settings** button in the top bar.

## Usage

1. **Configure** your repo in Settings (⚙)
2. **Create an agent** — click `+ New Agent`, give it a name and model
3. **Give it a task** — type in the chat, or paste a screenshot
4. **Watch it work** — tool calls stream in real time; the agent reads and writes files directly in your repo
5. **Ship** — push the branch, open a PR, or merge to main from the agent panel

## Security notes

- **Local only** — the server binds to `127.0.0.1:8000` and has no authentication. Do not expose it to the internet or a shared network.
- **`settings.json` is gitignored** — your GitHub token and repo path are never committed.
- **Agents run with `--dangerously-skip-permissions`** — Claude Code agents have unrestricted read/write access to your repository. Only point Agent Studio at repos you own.
- **CORS is open** — any page running in your browser can reach the API, which is fine for a local dev tool but another reason not to expose the port publicly.

## Tech stack

- **Backend** — FastAPI + uvicorn, streams Claude Code CLI output as SSE
- **Frontend** — React 18 (via CDN), Tailwind CSS, no build step
