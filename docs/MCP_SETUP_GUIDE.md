# DeLM Multi-Agent MCP Integration Guide

This guide explains how to connect **Claude Code**, **Codex**, **OpenCode (`oc`)**, and **Antigravity (`agy`)** to a shared decentralized DeLM board using the standard Model Context Protocol (MCP).

---

## 1. Overview

The DeLM MCP server enables multiple autonomous agent instances to coordinate asynchronously on the same codebase:
- **Task Reservation & Claims**: Agents atomically reserve scopes and claim sub-tasks so no two agents duplicate work.
- **Shared Knowledge Base**: Agents publish verified findings (`FACT`) and disproven hypotheses/blockers (`FAIL`).
- **File Diff Sharing**: Agents attach modified files or scripts upon task completion; peer agents can inspect diffs and apply them directly into their own workspaces.
- **Zero External Dependencies**: Pure Python standard library implementation over stdio JSON-RPC.

---

## 2. Install (one time)

The server is standard-library only: no third-party packages are ever needed.

**Option A — install the `delm-mcp` command (recommended):**
```bash
pipx install /path/to/DeLM
# or: pip install -e /path/to/DeLM
delm-mcp --version   # should print: delm-mcp 1.0.0
```

**Option B — run straight from the checkout (no install):**
```bash
python3 /path/to/DeLM/mcp_server/cli.py --version
```
No `PYTHONPATH` is required: the CLI adds the checkout root to `sys.path`
itself. Requires Python 3.10+.

## 3. Server Command

```bash
delm-mcp --actor <AGENT_NAME> --board <PATH_TO_SHARED_BOARD> --workspace <PATH_TO_WORKSPACE>
# without install:
python3 /path/to/DeLM/mcp_server/cli.py --actor <AGENT_NAME> --board <PATH_TO_SHARED_BOARD> --workspace <PATH_TO_WORKSPACE>
```

**Environment Variables Supported:**
* `DELM_ACTOR`: Name of the agent (e.g. `claude-1`, `codex-1`, `oc-worker`, `agy-worker`).
* `DELM_BOARD_PATH`: Path to the shared SQLite board (default: `.delm/board.sqlite`).
* `DELM_WORKSPACE`: Path to the local workspace root (default: current working directory).
* `DELM_BASELINE`: Optional pristine-checkout root used to diff peer files (see §6).
* `DELM_LOG_LEVEL`: Stderr log verbosity, one of `DEBUG INFO WARNING ERROR` (default: `INFO`).

---

## 4. Configuration by Client

Point every agent at the **same** `--board` file (e.g. `/shared/project/.delm/board.sqlite`).
Use a different `--actor` per agent. `--workspace` is each agent's own checkout.
If you installed `delm-mcp` (§2, Option A), use `"command": "delm-mcp"` and drop
the script path from `args`; otherwise use `"command": "python3"` with the
`cli.py` path as the first arg (no `PYTHONPATH` needed).

### A. Claude Code

Add the server to Claude Code using the `claude mcp` CLI or by editing `~/.claude/claude.json`:

```bash
claude mcp add delm -- delm-mcp \
  --actor claude-1 \
  --board /shared/project/.delm/board.sqlite \
  --workspace .
```

Or in `claude.json`:
```json
{
  "mcpServers": {
    "delm": {
      "command": "delm-mcp",
      "args": [
        "--actor", "claude-1",
        "--board", "/shared/project/.delm/board.sqlite",
        "--workspace", "."
      ]
    }
  }
}
```

---

### B. OpenAI Codex

In your Codex configuration file (e.g., `~/.codex/config.json` or project-level config):

```json
{
  "mcpServers": {
    "delm": {
      "command": "delm-mcp",
      "args": [
        "--actor", "codex-1",
        "--board", "/shared/project/.delm/board.sqlite",
        "--workspace", "."
      ]
    }
  }
}
```

---

### C. OpenCode (`oc`)

In OpenCode's configuration (e.g. `~/.config/opencode/mcp.json` or project `.opencode.json`):

```json
{
  "mcp": {
    "delm": {
      "command": "delm-mcp",
      "args": [
        "--actor", "oc-worker",
        "--board", "/shared/project/.delm/board.sqlite",
        "--workspace", "."
      ]
    }
  }
}
```

---

### D. Antigravity (`agy`)

Add the DeLM server to your Antigravity settings in `~/.gemini/settings.json` under `"mcpServers"`:

```json
{
  "mcpServers": {
    "delm": {
      "command": "delm-mcp",
      "args": [
        "--actor", "agy-agent",
        "--board", "/shared/project/.delm/board.sqlite",
        "--workspace", "."
      ]
    }
  }
}
```

---

## 5. Exposed Tools

Agents connected to the DeLM MCP server gain access to these 9 coordination tools:

1. `delm_read_board`: Read current tasks, peer notes, and status updates (supports optional waiting with `wait_seconds` up to 25, and `target` to wait for a specific item).
2. `delm_reserve_task`: Declare a new scope and atomically claim it.
3. `delm_publish_items`: Publish open queue items for any peer to claim (the initial split of work).
4. `delm_claim_task`: Claim an existing open task by ID.
5. `delm_complete_task`: Mark a task complete with a solution summary and optional attached files. Accepts `request_id` for safe retries.
6. `delm_publish_note`: Share a verified finding (`FACT`) or a disproven approach/blocker (`FAIL`). Accepts `request_id` for safe retries.
7. `delm_update_status`: Publish current agent focus or blockers.
8. `delm_inspect_files`: Preview diffs (against the baseline, §6) and file changes published by peers.
9. `delm_apply_files`: Merge a peer's published files into the local workspace. Files you edited locally are reported as conflicts and **nothing is written** until you reconcile them.

---

## 6. Baseline diffs (`--baseline`)

`delm_inspect_files` shows unified diffs for files that differ from a pristine
baseline. Each agent's workspace changes as it works, so pass a clean checkout
as the baseline to get meaningful diffs:

```bash
delm-mcp --actor codex-1 --board /shared/project/.delm/board.sqlite \
  --workspace /shared/project/work-codex --baseline /shared/project/pristine
```

Without `--baseline`, modified files compare against the agent's own workspace
(diff appears empty / "unchanged") and brand-new files are shown in full.
Either way, `delm_apply_files` still merges correctly.

---

## 7. Troubleshooting

| Symptom | Cause / Fix |
| :--- | :--- |
| `ModuleNotFoundError: No module named 'board'` | Old config using `cli.py` without the checkout on `sys.path`. Fix: install `delm-mcp` (§2, Option A) and use `"command": "delm-mcp"`, or keep `"command": "python3"` with the `cli.py` path (v1.0.0 needs no `PYTHONPATH`). |
| Server exits immediately / client says "connection closed" | Check stderr: bad `--actor` (letters, digits, `.` `_` `-` only), missing `--workspace` dir, or unwritable `--board` parent. Run `delm-mcp --version` and the self-check below. |
| Tools list is empty / handshake fails | Client and server protocol versions: the server negotiates `2024-11-05`, `2025-03-26`, `2025-06-18`. Update the client, then re-add the MCP server. |
| Agents don't see each other | Each agent must use the **same** `--board` path and a **unique** `--actor`. A quick check: both boards' `seq` in `delm_read_board` output should advance together. |
| `delm_apply_files` reports CONFLICT | Expected safety behavior, not a bug: inspect the peer version with `delm_inspect_files`, reconcile locally, retry. Nothing was overwritten. |
| Board file grows forever | The board is an append-only log. Archive it between runs: stop agents, move `.delm/board.sqlite*` aside, restart. |

**Self-check** (verifies install, protocol, and all 9 tools in one shot):

```bash
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}' \
  | delm-mcp --actor diag --board /tmp/delm-diag.sqlite --workspace /tmp
```

**Test suite:**

```bash
python3 -m unittest tests/test_mcp_server.py -v
```

---

## 8. Running in Docker

The server ships as a minimal image (`python:3.12-slim`, no third-party packages).
Build it from the repository root:

```bash
docker build -f mcp_server/Dockerfile -t delm-mcp:1.0.0 .
```

Each agent runs its own container against the **same** board file via a bind
mount, with its own `--actor` and `--workspace`:

```bash
# One shared board dir + one workspace dir per agent (must be writable
# by the container user, uid 10001):
mkdir -p /shared/project/.delm /shared/project/work-codex
chmod a+rwX /shared/project/.delm /shared/project/work-codex

docker run -i --rm \
  -v /shared/project/.delm:/data \
  -v /shared/project/work-codex:/work \
  delm-mcp:1.0.0 \
  --actor codex-1 --board /data/board.sqlite --workspace /work
```

(Alternative to `chmod`: `chown -R 10001 /shared/project/.delm`, or add
`--user $(id -u):$(id -g)` to run as yourself.)

Point a client at the container (Codex `~/.codex/config.json` shown; same shape
for Claude Code, OpenCode, Antigravity):

```json
{
  "mcpServers": {
    "delm": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/shared/project/.delm:/data",
        "-v", "/shared/project/work-codex:/work",
        "delm-mcp:1.0.0",
        "--actor", "codex-1",
        "--board", "/data/board.sqlite",
        "--workspace", "/work"
      ]
    }
  }
}
```
