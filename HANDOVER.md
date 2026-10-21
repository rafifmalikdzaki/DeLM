# Handover: DeLM Multi-Agent MCP Server Integration

## Executive Summary
This fork of [DeLM (Decentralized Multi-Agent Systems with Shared Context)](https://github.com/yuzhenmao/DeLM) extends the core coordination engine with a zero-dependency **Model Context Protocol (MCP)** server. 

Previously, DeLM agents were only accessible via specialized Docker evaluation harnesses and shell invocations (`context.sh`). With this integration, agents in **Claude Code**, **Codex**, **OpenCode (`oc`)**, and **Antigravity (`agy`)** can interact directly with the decentralized shared-context board via standard MCP tool calls.

---

## Repository & Remote Configuration

- **Fork Remote (`origin`)**: `git@github.com:rafifmalikdzaki/DeLM.git`
- **Upstream Remote (`upstream`)**: `git@github.com:yuzhenmao/DeLM.git`
- **Active Branch**: `main`

To sync with upstream changes in the future:
```bash
git fetch upstream
git merge upstream/main
```

---

## Key Components Added

### 1. Model Context Protocol Server (`mcp_server/`)
- [`mcp_server/__init__.py`](file:///home/dzakirm/AI-Tools/DeLM/mcp_server/__init__.py): Package initialization.
- [`mcp_server/server.py`](file:///home/dzakirm/AI-Tools/DeLM/mcp_server/server.py): Standard IO JSON-RPC MCP server. Uses Python standard library only (`sqlite3`, `sys`, `json`, `difflib`).
  Directly interfaces with DeLM's core [`SharedContext`](file:///home/dzakirm/AI-Tools/DeLM/board/shared_context.py) engine.
- [`mcp_server/cli.py`](file:///home/dzakirm/AI-Tools/DeLM/mcp_server/cli.py): Executable CLI entrypoint with support for CLI flags (`--actor`, `--board`, `--workspace`, `--peers`) and fallback environment variables (`DELM_ACTOR`, `DELM_BOARD_PATH`, `DELM_WORKSPACE`).

### 2. Coordination Tools Exposed to LLM Agents
1. `delm_read_board`: Read current tasks, peer notes, and status updates (supports polling/waiting).
2. `delm_reserve_task`: Declare a new scope and atomically claim it to avoid duplicate work.
3. `delm_claim_task`: Claim an existing open task by ID.
4. `delm_complete_task`: Mark a task as done, publish a solution summary, and attach workspace files.
5. `delm_publish_note`: Publish verified findings (`FACT`) or disproven hypotheses/blockers (`FAIL`).
6. `delm_update_status`: Publish current agent focus or blockers.
7. `delm_inspect_files`: Preview diffs and file changes published by peer agents (`expand`).
8. `delm_apply_files`: Write a peer's published files into the local workspace (`apply`).

### 3. Setup Documentation (`docs/`)
- [`docs/MCP_SETUP_GUIDE.md`](file:///home/dzakirm/AI-Tools/DeLM/docs/MCP_SETUP_GUIDE.md): Detailed configuration guides for:
  - Claude Code (`claude mcp add` / `claude.json`)
  - OpenAI Codex (`config.json`)
  - OpenCode / `oc` (`mcp.json`)
  - Antigravity / `agy` (`~/.gemini/settings.json`)

### 4. Test Suite (`tests/`)
- [`tests/test_mcp_server.py`](file:///home/dzakirm/AI-Tools/DeLM/tests/test_mcp_server.py): Unit and multi-agent coordination integration tests verifying:
  - Protocol handshake (`initialize`, `tools/list`).
  - Concurrent agents claiming tasks, sharing notes, completing tasks with file attachments, and applying peer files.

---

## How to Verify
Run the test suite:
```bash
PYTHONPATH=. python3 -m unittest tests/test_mcp_server.py -v
```

Test stdio framing directly:
```bash
echo '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}}' | PYTHONPATH=. python3 mcp_server/cli.py --actor tester --board /tmp/test_board.sqlite --workspace /tmp
```

---

## Next Steps & Potential Extensions
1. **Networked / SSE Transport**: Optionally add Server-Sent Events (SSE) or WebSocket transport if agents run across different physical machines rather than sharing a local filesystem.
2. **Auto-Cleanup / Ephemeral Sessions**: Add an automated prune/compact command for lingering SQLite event databases between independent runs.
3. **IDE Notification Hooks**: Integrate board events with desktop notifications or webhooks when a peer completes a dependent task.

---

## v1.0.0 Production Hardening (2026-10-20)

Two correctness bugs were reproduced and fixed, and the server was hardened for real multi-agent use:

- **Fixed `delm_inspect_files` crash**: it iterated the `expand` dict as a list (`TypeError: string indices must be integers`) and read a nonexistent `diff` key. It now renders `text`/`shown` per file with an output cap.
- **Fixed `delm_apply_files` data loss**: it bypassed the board's apply logic and overwrote local files unconditionally. It now uses `ProtectedBoard.apply` with real local hashes (conflicts refuse with nothing written), atomic stage-and-rename writes with rollback, and `apply_result`/`apply_error` audit events.
- **Correctness/safety**: `ProtectedBoard` instead of `SharedContext`; workspace confinement (`..`, symlink, credential/cache rejection ported from `board/context_client.py`); generous input length caps; `request_id` idempotency on done/note; new `delm_publish_items` tool; `--baseline` for real diffs; WAL/busy-timeout tuning; structured stderr logging with `--log-level`.
- **Protocol**: version negotiation (`2024-11-05`, `2025-03-26`, `2025-06-18`), empty `resources/list`/`prompts/list`, notification/batch support, JSON-RPC error codes, Content-Length + NDJSON framing.
- **Packaging/CLI**: `pyproject.toml` with `delm-mcp` entry point; `python -m mcp_server`; actor/workspace/board validation; SIGTERM graceful shutdown.
- **Tests**: `tests/test_mcp_server.py` grew from 2 to 31 tests (protocol, validation, publish/claim, baseline diffs, conflict apply, path safety, concurrent reserve, idempotent retries, CLI wiring). Run: `python3 -m unittest tests/test_mcp_server.py -v`.

Key files: `mcp_server/server.py`, `mcp_server/files.py` (new), `mcp_server/cli.py`, `mcp_server/__main__.py` (new), `pyproject.toml` (new), `docs/MCP_SETUP_GUIDE.md` (install + troubleshooting), `docs/MCP_ARCHITECTURE.md`, `README.md`.

### Follow-up hardening (same week)

- **Request-id collision**: the derived default id hashed only attach *paths*; re-running a note with updated file content was rejected (`request_id was already used`). Default ids now hash file contents, so true retries replay while new content publishes. Guarded by `test_same_text_new_content_creates_new_publication`.
- **Truncated board ids**: `format_view` cut item ids to 8 chars, so a peer reading the board could not call inspect/apply. Full item and note ids are now rendered. Guarded by `test_board_text_carries_usable_ids`.
- **Silent WAL misconfig**: `bodydb.PRAGMA journal_mode=WAL` is invalid SQL and silently skipped bodies-DB tuning. Fixed to `PRAGMA bodydb.journal_mode=WAL` (+ busy timeout). Guarded by `test_both_databases_use_wal`.
- **Storage resilience**: `call_tool` retries transient SQLITE_BUSY with backoff and converts other `sqlite3.Error`s into clean board errors; CLI rejects a `--board` pointing at a directory (exit 2). Guarded by `test_board_directory_rejected`.
- **Containerized**: `mcp_server/Dockerfile` (`python:3.12-slim`, non-root uid 10001, `delm-mcp` entrypoint) added; verified two containers coordinating (reserve → complete+attach → read → inspect → apply) over a bind-mounted board. Client `docker run -i` config documented in `docs/MCP_SETUP_GUIDE.md` §8.
- No DeLM-repo MCP processes were running (the 13 matches were Harness²'s `mcp_server.py`, a different project — left untouched).
- Tests: 36 passing (`python3 -m unittest tests/test_mcp_server.py`).

### Harness²-inspired hardening (2026-10-21)

All five improvements, each with regression tests; 66 tests green across `test_mcp_server.py`, `test_mercury_worker.py`, `test_worktree.py`, `test_evolve.py`:

1. **Worktree peer spawning** (`runtime/worktree.py`, new): `spawn_peer`/`remove_peer`/`list_peers` isolate local agents in **detached, zero-branch** git worktrees (mirrors the Harness² fix — no `delm/<session>/<actor>` branches are ever registered). 7 tests including `test_no_branches_ever_registered`.
2. **Hygiene**: extended attach/apply exclusions (`.harness2`, `.harness_staged`, `.pytest_cache`, `.pyc/.pyo`, `.DS_Store`, `.delm/`, `.board/`) — caught a real case bug (`.ds_store` vs `.DS_Store`) — plus opt-in `--git-exclude` flag that appends `.delm/` to workspace `.git/info/exclude` (never `.gitignore`, off by default).
3. **Conflict markers**: `delm_apply_files` accepts `on_conflict: "markers"` to write git-style `<<<<<<< LOCAL` / `>>>>>>> PEER` blocks instead of refusing; default `refuse` unchanged.
4. **Peer verification** (`delm_verify_task`, new tool #10): records independent accept/reject with evidence on completed items. Advisory only, author excluded, new `verify` event kind in `SharedContext` (view + incremental serve; never wakes waits; harness call sites untouched).
5. **Lessons** (`workers/evolve.py`, new): harvests FACT/FAIL notes into persistent `.delm/knowledge.db` (dedupe by fingerprint) and prints a seed block for the next session.
- Docs: setup guide (§5 tools, flags, §8 Docker), architecture table, README (10 tools).

### Mercury-3 board worker (2026-10-21)

- `workers/mercury_board_worker.py` (new, stdlib only): reserves one board scope, asks Mercury-3 (`POST https://api.inceptionlabs.ai/v1/chat/completions`, OpenAI-compatible, verified against official docs) for an implementation analysis, publishes it as a `FACT` note, and completes the item. Never writes model output to files.
- Key handling: `INCEPTION_API_KEY` from environment or gitignored local `.env`, never printed/logged/committed. The key pasted in chat should be rotated.
- Verified: 7 mocked offline tests (`tests/test_mercury_worker.py`, zero spend) plus a live end-to-end run on a scratch board (1,483 tokens: reserve → FACT note → done, all visible via `delm_read_board`). Usage: `docs/MERCURY_WORKER.md`.
