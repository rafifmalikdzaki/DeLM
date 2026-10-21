"""DeLM MCP server: stdio JSON-RPC bridge to the decentralized shared board.

Requires only the Python standard library. Each agent process runs one
instance of :class:`DelmMcpServer` pointed at the same SQLite board file;
agents coordinate through task claims, FACT/FAIL notes and file publications
without a central orchestrator.

Supported MCP clients: Claude Code, OpenAI Codex, OpenCode (``oc``) and
Antigravity (``agy``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from board.protected_board import ProtectedBoard
from board.shared_context import BoardError, WAIT_CAP_SECONDS

from mcp_server import __version__
from mcp_server.files import atomic_write_files, bundle_files, local_hashes, locate

log = logging.getLogger("delm-mcp")

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

ACTOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Generous hard caps (characters) that keep board entries readable and bound
# the context shipped back to the model. The harness prompts suggest shorter
# targets (subject ~80, detail ~300, note text ~400); these caps only reject
# pathological inputs.
MAX_SUBJECT_CHARS = 200
MAX_DETAIL_CHARS = 2000
MAX_SUMMARY_CHARS = 4000
MAX_NOTE_TEXT_CHARS = 2000
MAX_SCOPE_CHARS = 300
MAX_BASIS_CHARS = 2000
MAX_STATUS_CHARS = 500
MAX_PUBLISH_ITEMS = 20
MAX_INSPECT_CHARS = 60_000

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def validate_actor_name(actor: str) -> str:
    if not isinstance(actor, str) or not ACTOR_PATTERN.fullmatch(actor):
        raise ValueError(
            f"invalid actor name {actor!r}: use 1-64 letters, digits, '.', '_' or '-'"
        )
    return actor


def checked_text(field: str, value: Any, max_chars: int) -> str:
    """Validate a required board text field; rejects empty and oversized input."""
    if not isinstance(value, str) or not value.strip():
        raise BoardError(f"{field} must be non-empty text")
    if len(value) > max_chars:
        raise BoardError(
            f"{field} is {len(value):,} characters; at most {max_chars:,} allowed"
        )
    return value


def checked_id(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoardError(f"{field} must be a non-empty id string")
    return value.strip()


def format_view(view: Dict[str, Any]) -> str:
    """Format a board view into a readable markdown summary for the agent."""
    lines = [f"### DeLM Shared Board (seq: {view.get('sequence', 0)})"]

    queue = view.get("queue", [])
    lines.append(f"\n**Task Queue ({len(queue)} items):**")
    if not queue:
        lines.append("- (No tasks on the board)")
    shown_queue = queue[:50]
    for item in shown_queue:
        status_indicator = "✅ [DONE]" if item.get("state") == "done" else (
            f"⏳ [CLAIMED by {item.get('owner')}]" if item.get("state") == "claimed" else "⚪ [OPEN]"
        )
        files_info = f" ({len(item.get('files', []))} files attached)" if item.get("files") else ""
        # Full ids are shown: they are the handles for claim/inspect/apply.
        lines.append(f"- `{item.get('id', '')}` {status_indicator} **{item.get('subject', 'Untitled')}**{files_info}")
        if item.get("detail"):
            lines.append(f"  *Detail:* {item.get('detail')}")
        if item.get("summary"):
            lines.append(f"  *Summary:* {item.get('summary')}")
    if len(queue) > len(shown_queue):
        lines.append(f"- ... and {len(queue) - len(shown_queue)} more (use full read to page)")

    notes = view.get("notes", [])
    if notes:
        lines.append(f"\n**Recent Discoveries / Notes ({len(notes)}):**")
        for note in notes[:30]:
            note_type = note.get("type", "NOTE")
            icon = "💡 [FACT]" if note_type == "FACT" else "⚠️ [FAIL]"
            author = note.get("actor", "unknown")
            files_info = f" (attached: {', '.join(f.get('path', '') for f in note.get('files', []))})" if note.get("files") else ""
            lines.append(f"- {icon} **{author}** on `{note.get('scope', 'general')}`: {note.get('text', '')}{files_info}")
            lines.append(f"  *id:* `{note.get('id', '')}`")
            if note.get("basis"):
                lines.append(f"  *Evidence/Basis:* {note.get('basis')}")
        if len(notes) > 30:
            lines.append(f"- ... and {len(notes) - 30} more notes")

    statuses = view.get("status", [])
    if statuses:
        lines.append("\n**Agent Working States:**")
        for s in statuses:
            lines.append(f"- **{s.get('actor')}**: {s.get('text')}")

    verifications = view.get("verifications", [])
    if verifications:
        subjects = {i.get("id"): i.get("subject", "?") for i in view.get("queue", [])}
        lines.append(f"\n**Peer Verifications ({len(verifications)}):**")
        for v in verifications:
            icon = "✅" if v.get("verdict") == "accept" else "❌"
            lines.append(
                f"- {icon} **{v.get('actor')}** {v.get('verdict')}d "
                f"`{v.get('item', '')}` ({subjects.get(v.get('item'), '?')}): "
                f"{v.get('evidence', '')}"
            )

    return "\n".join(lines)


def ok_result(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def err_result(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class DelmMcpServer:
    """MCP stdio server backed by DeLM's protected shared-context board."""

    GIT_EXCLUDE_LINES = (".delm/",)

    def __init__(
        self,
        actor: str,
        board_path: Path,
        workspace_path: Path,
        peers: Optional[List[str]] = None,
        baseline_path: Optional[Path] = None,
        git_exclude: bool = False,
    ):
        self.actor = validate_actor_name(actor)
        self.board_path = Path(board_path).resolve()
        self.workspace_path = Path(workspace_path).resolve()
        self.baseline_path = Path(baseline_path).resolve() if baseline_path else None
        if git_exclude:
            self._ensure_git_exclude()
        if peers:
            self.peers = list(dict.fromkeys(peers))
        else:
            self.peers = [self.actor]
        if self.actor not in self.peers:
            self.peers.append(self.actor)

        self.board_path.parent.mkdir(parents=True, exist_ok=True)
        self.sc = ProtectedBoard(str(self.board_path), self.peers, original=self._get_original_file)
        self._enable_wal()

    def _enable_wal(self) -> None:
        """Best-effort concurrent-access tuning; the board works without it."""
        try:
            db = sqlite3.connect(str(self.board_path), timeout=10)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=10000")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("ATTACH DATABASE ? AS bodydb", (str(self.sc.bodies_path),))
                db.execute("PRAGMA bodydb.journal_mode=WAL")
                db.execute("PRAGMA bodydb.busy_timeout=10000")
                db.execute("PRAGMA bodydb.synchronous=NORMAL")
            finally:
                db.close()
        except Exception as error:  # pragma: no cover - tuning only
            log.debug("WAL tuning skipped: %s", error)

    def stop(self) -> None:
        """Release any waiting read so the server can shut down promptly."""
        self.sc.stop()

    def _ensure_git_exclude(self) -> None:
        """Opt-in: keep the board dir out of the workspace's git status.

        Appends missing lines to ``.git/info/exclude`` (never touches
        ``.gitignore``). Best-effort: any failure is logged, never raised,
        so a non-repo workspace starts normally.
        """
        try:
            git_dir = self.workspace_path / ".git"
            if not git_dir.is_dir():
                return
            exclude = git_dir / "info" / "exclude"
            existing = (exclude.read_text(encoding="utf-8").splitlines()
                        if exclude.is_file() else [])
            missing = [l for l in self.GIT_EXCLUDE_LINES if l not in existing]
            if not missing:
                return
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and not existing[-1].strip() == "":
                    handle.write("\n")
                handle.write("".join(f"{line}\n" for line in missing))
            log.info("added %s to %s", ", ".join(missing), exclude)
        except OSError as error:  # pragma: no cover - best effort only
            log.warning("git exclude update skipped: %s", error)

    # ---- workspace access ------------------------------------------------

    def _get_original_file(self, path: str) -> Optional[bytes]:
        """Baseline content for diffing: pristine checkout, else own workspace."""
        for root in (self.baseline_path, self.workspace_path):
            if root is None:
                continue
            target = root / path
            try:
                if target.is_file() and not target.is_symlink():
                    return target.read_bytes()
            except OSError:
                return None
        return None

    def _attach_files(self, attach_paths: Any) -> Optional[List[Dict[str, Any]]]:
        if not attach_paths:
            return None
        if not isinstance(attach_paths, list) or not all(isinstance(p, str) for p in attach_paths):
            raise BoardError("attach_paths must be a list of file path strings")
        return bundle_files(attach_paths, self.workspace_path)

    @staticmethod
    def _default_request_id(payload: Dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    @staticmethod
    def _content_identities(files: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Content-sensitive file identities so retries replay but new content republishes."""
        return sorted(
            (
                {
                    "path": entry["path"],
                    "sha256": hashlib.sha256(
                        entry["content"].encode("utf-8")
                    ).hexdigest(),
                }
                for entry in files
            ),
            key=lambda item: item["path"],
        )

    def _request_id(self, arguments: Dict[str, Any], payload: Dict[str, Any]) -> str:
        request_id = arguments.get("request_id")
        if request_id is None:
            return self._default_request_id(payload)
        if not isinstance(request_id, str) or not request_id.strip():
            raise BoardError("request_id must be a non-empty string")
        return request_id.strip()

    # ---- tool schemas ----------------------------------------------------

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return the MCP tool schemas exposed to the agent."""
        return [
            {
                "name": "delm_read_board",
                "description": "Read the shared DeLM coordination board: claimed/open tasks, peer FACT/FAIL notes, and agent states. Pass wait_seconds (up to 25) to wait for peer activity instead of polling.",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "wait_seconds": {
                            "type": "number",
                            "description": "Seconds to wait (0-25) if no new events exist. 0 for an immediate non-blocking read.",
                            "default": 0,
                        },
                        "full": {
                            "type": "boolean",
                            "description": "Return the full board instead of only changes since the last read.",
                            "default": False,
                        },
                        "target": {
                            "type": "string",
                            "description": "Optional item or publication id you are waiting for; the read returns early once it appears.",
                        },
                    },
                },
            },
            {
                "name": "delm_reserve_task",
                "description": "Atomically create and claim one work scope so peer agents avoid duplicate work. Subjects match ignoring case and whitespace; retries report the existing owner.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "Short title of the scope (e.g. 'Implement database migration runner')",
                            "maxLength": MAX_SUBJECT_CHARS,
                        },
                        "detail": {
                            "type": "string",
                            "description": "Inputs, outputs, expected interface, and dependency requirements.",
                            "maxLength": MAX_DETAIL_CHARS,
                        },
                    },
                    "required": ["subject", "detail"],
                },
            },
            {
                "name": "delm_publish_items",
                "description": "Publish open queue items for any agent to claim (e.g. the initial split of work). Publishing does not claim ownership; matching subjects reuse existing items.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "description": "Up to 20 items, each with a subject and detail.",
                            "maxItems": MAX_PUBLISH_ITEMS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "subject": {"type": "string", "maxLength": MAX_SUBJECT_CHARS},
                                    "detail": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
                                },
                                "required": ["subject", "detail"],
                            },
                        },
                    },
                    "required": ["items"],
                },
            },
            {
                "name": "delm_claim_task",
                "description": "Claim an existing open task from the board by ID. Fails when the item is already claimed or done, or when you already hold another item.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID of the open task to claim",
                        },
                    },
                    "required": ["item_id"],
                },
            },
            {
                "name": "delm_complete_task",
                "description": "Mark your claimed task done with a solution summary and optional workspace file attachments. Repeating the call with the same request_id returns the original publication.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "The item ID you previously claimed",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Results, changed functions, limitation notes, and test outcomes.",
                            "maxLength": MAX_SUMMARY_CHARS,
                        },
                        "attach_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Workspace-relative text files to publish to the board.",
                        },
                        "request_id": {
                            "type": "string",
                            "description": "Optional idempotency key; retries with the same key return the first publication.",
                        },
                    },
                    "required": ["item_id", "summary"],
                },
            },
            {
                "name": "delm_publish_note",
                "description": "Publish a verified finding (FACT) or disproven approach/blocker (FAIL) with reproducible evidence so peers learn without repeating the work.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "note_type": {
                            "type": "string",
                            "enum": ["FACT", "FAIL"],
                            "description": "FACT for verified observation, FAIL for contradicted hypothesis.",
                        },
                        "text": {
                            "type": "string",
                            "description": "The finding or blocker description",
                            "maxLength": MAX_NOTE_TEXT_CHARS,
                        },
                        "scope": {
                            "type": "string",
                            "description": "Component or area impacted",
                            "maxLength": MAX_SCOPE_CHARS,
                        },
                        "basis": {
                            "type": "string",
                            "description": "Reproducible evidence: commands run, inputs, and measured results.",
                            "maxLength": MAX_BASIS_CHARS,
                        },
                        "attach_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional files (reproduction scripts, benchmark logs) to attach.",
                        },
                        "request_id": {
                            "type": "string",
                            "description": "Optional idempotency key for safe retries.",
                        },
                    },
                    "required": ["note_type", "text", "scope", "basis"],
                },
            },
            {
                "name": "delm_verify_task",
                "description": "Independently verify a peer's completed item: re-check the diff against the requirements and record accept/reject with evidence. Advisory only; never changes ownership. You cannot verify your own item.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID of the completed item to verify",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["accept", "reject"],
                            "description": "accept when the work holds up, reject with concrete counter-evidence otherwise",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "What you checked and the outcome (tests run, diff reviewed)",
                            "maxLength": 2000,
                        },
                    },
                    "required": ["item_id", "verdict", "evidence"],
                },
            },
            {
                "name": "delm_update_status",
                "description": "Broadcast your current focus (e.g. 'Investigating race condition in auth.py').",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Brief description of what you are investigating or doing",
                            "maxLength": MAX_STATUS_CHARS,
                        },
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "delm_inspect_files",
                "description": "Preview files attached to a peer publication: unified diffs against the baseline for modified files, full text for new files.",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "publication_id": {
                            "type": "string",
                            "description": "Publication ID (from a completion or note) or the queue item ID",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of attached file paths to view in full.",
                        },
                    },
                    "required": ["publication_id"],
                },
            },
            {
                "name": "delm_apply_files",
                "description": "Merge a peer publication's files into your workspace. Files you changed locally (or that another author changed) are reported as conflicts and nothing is written until you reconcile them.",
                "annotations": {"destructiveHint": True},
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "publication_id": {
                            "type": "string",
                            "description": "Publication ID containing the files to pull",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of file paths to apply. If omitted, applies all files in the publication.",
                        },
                        "on_conflict": {
                            "type": "string",
                            "enum": ["refuse", "markers"],
                            "description": "How to handle files you changed locally: 'refuse' (default) writes nothing; 'markers' writes git-style conflict blocks so you can merge by hand.",
                            "default": "refuse",
                        },
                    },
                    "required": ["publication_id"],
                },
            },
        ]

    # ---- tool dispatch ---------------------------------------------------

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool call, returning MCP content blocks."""
        started = time.monotonic()
        try:
            result = self._dispatch_with_retry(name, arguments or {})
        except BoardError as error:
            result = err_result(f"DeLM Board Error: {error}")
        except Exception as error:  # pragma: no cover - defensive
            log.exception("Unexpected error in tool %s", name)
            result = err_result(f"Unexpected Error: {error}")
        log.info(
            "tool=%s actor=%s isError=%s elapsed=%.3fs",
            name, self.actor, result.get("isError"), time.monotonic() - started,
        )
        return result

    def _dispatch_with_retry(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Run the tool, retrying transient SQLite lock contention.

        Concurrent writers can hit SQLITE_BUSY despite the connect timeout
        (slow disks, network filesystems, many agents). A busy failure means
        the statement never ran, so retrying with backoff is safe; anything
        else from storage becomes a clean BoardError instead of an
        "Unexpected Error" leak.
        """
        last_error: Optional[Exception] = None
        for attempt in range(4):
            try:
                return self._dispatch(name, arguments)
            except BoardError:
                raise
            except sqlite3.OperationalError as error:
                last_error = error
                if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                    break
                log.warning(
                    "tool=%s board busy (attempt %d/4), retrying", name, attempt + 1
                )
                time.sleep(0.05 * (2 ** attempt))
            except sqlite3.Error as error:
                last_error = error
                break
        raise BoardError(
            f"board storage temporarily unavailable ({last_error}); retry the call"
        )

    def _dispatch(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "delm_read_board":
            raw_wait = arguments.get("wait_seconds", 0)
            try:
                wait = float(raw_wait)
            except (TypeError, ValueError):
                raise BoardError("wait_seconds must be a number") from None
            wait = max(0.0, min(wait, WAIT_CAP_SECONDS))
            full = bool(arguments.get("full", False))
            target = arguments.get("target") or None
            if target is not None:
                target = checked_id("target", target)
            view = self.sc.read(self.actor, full=full, wait_seconds=wait, target=target)
            if view.get("updates") is False:
                return ok_result(
                    f"No updates after {view.get('waited', 0):.1f}s "
                    f"(board seq {view.get('sequence', 0)}). "
                    "Your last served view is still current."
                )
            text = format_view(view)
            if view.get("waited"):
                text = (
                    f"Update after {view.get('waited', 0):.1f}s "
                    f"(woke_by={view.get('woke_by')}).\n\n" + text
                )
            return ok_result(text)

        elif name == "delm_reserve_task":
            subject = checked_text("subject", arguments.get("subject"), MAX_SUBJECT_CHARS)
            detail = checked_text("detail", arguments.get("detail"), MAX_DETAIL_CHARS)
            res = self.sc.reserve(self.actor, subject, detail)
            status = "Won claim" if res.get("won") else f"Already owned by {res.get('owner')}"
            return ok_result(
                f"{status} on task `{res.get('item')}`: **{subject}**\n\n"
                f"{format_view(res.get('view', {}))}"
            )

        elif name == "delm_publish_items":
            items = arguments.get("items")
            if not isinstance(items, list) or not items:
                raise BoardError("items must be a non-empty list")
            if len(items) > MAX_PUBLISH_ITEMS:
                raise BoardError(
                    f"{len(items)} items given; at most {MAX_PUBLISH_ITEMS} per call"
                )
            checked = []
            for entry in items:
                if not isinstance(entry, dict):
                    raise BoardError("each item must have a subject and detail")
                checked.append({
                    "subject": checked_text("item_subject", entry.get("subject"), MAX_SUBJECT_CHARS),
                    "detail": checked_text("item_detail", entry.get("detail"), MAX_DETAIL_CHARS),
                })
            res = self.sc.publish(self.actor, checked)
            lines = []
            for entry, out in zip(checked, res["items"]):
                verb = "created" if out.get("created") else f"reused (state={out.get('state')})"
                owner = f" owner={out['owner']}" if out.get("owner") else ""
                lines.append(f"- `{out['id'][:8]}` {verb}{owner}: **{entry['subject']}**")
            return ok_result("Published queue items:\n" + "\n".join(lines))

        elif name == "delm_claim_task":
            item_id = checked_id("item_id", arguments.get("item_id"))
            res = self.sc.claim(self.actor, item_id)
            if res.get("won"):
                return ok_result(
                    f"Claimed item `{item_id}`.\n\n{format_view(res.get('view', {}))}"
                )
            return err_result(
                f"Could not claim `{item_id}` (state={res.get('state')}, "
                f"owner={res.get('owner')}).\n\n{format_view(res.get('view', {}))}"
            )

        elif name == "delm_complete_task":
            item_id = checked_id("item_id", arguments.get("item_id"))
            summary = checked_text("summary", arguments.get("summary"), MAX_SUMMARY_CHARS)
            files = self._attach_files(arguments.get("attach_paths"))
            request_id = self._request_id(
                arguments, {"kind": "done", "item": item_id, "summary": summary,
                            "files": self._content_identities(files) if files else []},
            )
            res = self.sc.done(self.actor, item_id, summary, files=files, request_id=request_id)
            attached = len(files) if files else 0
            return ok_result(
                f"Completed task `{item_id}`. Publication ID: `{res.get('publication_id')}` "
                f"({attached} files attached).\n\n{format_view(res.get('view', {}))}"
            )

        elif name == "delm_publish_note":
            note_type = arguments.get("note_type")
            if note_type not in ("FACT", "FAIL"):
                raise BoardError("note_type must be either 'FACT' or 'FAIL'")
            text = checked_text("text", arguments.get("text"), MAX_NOTE_TEXT_CHARS)
            scope = checked_text("scope", arguments.get("scope"), MAX_SCOPE_CHARS)
            basis = checked_text("basis", arguments.get("basis"), MAX_BASIS_CHARS)
            files = self._attach_files(arguments.get("attach_paths"))
            request_id = self._request_id(
                arguments, {"kind": "note", "type": note_type, "text": text,
                            "scope": scope, "basis": basis,
                            "files": self._content_identities(files) if files else []},
            )
            res = self.sc.note(
                self.actor, note_type, text, scope, basis,
                files=files, request_id=request_id,
            )
            attached = len(files) if files else 0
            return ok_result(
                f"Published {note_type} `{res.get('publication_id')}` on scope '{scope}' "
                f"({attached} files attached).\n\n{format_view(res.get('view', {}))}"
            )

        elif name == "delm_verify_task":
            item_id = checked_id("item_id", arguments.get("item_id"))
            verdict = arguments.get("verdict")
            if verdict not in ("accept", "reject"):
                raise BoardError("verdict must be either 'accept' or 'reject'")
            evidence = checked_text("evidence", arguments.get("evidence"), 2000)
            res = self.sc.verify(self.actor, item_id, verdict, evidence)
            return ok_result(
                f"Recorded {verdict} on `{item_id}` "
                f"(verification `{res.get('verification_id')}`).\n\n"
                f"{format_view(res.get('view', {}))}"
            )

        elif name == "delm_update_status":
            text = checked_text("status", arguments.get("text"), MAX_STATUS_CHARS)
            res = self.sc.status(self.actor, text)
            return ok_result(
                f"Status updated to: '{text}'.\n\n{format_view(res.get('view', {}))}"
            )

        elif name == "delm_inspect_files":
            pub_id = checked_id("publication_id", arguments.get("publication_id"))
            paths = arguments.get("paths")
            board_paths = self._board_path_subset(paths)
            rendered = self.sc.expand(self.actor, pub_id, paths=board_paths)
            return ok_result(self._render_expanded(rendered))

        elif name == "delm_apply_files":
            pub_id = checked_id("publication_id", arguments.get("publication_id"))
            paths = arguments.get("paths")
            strategy = arguments.get("on_conflict", "refuse")
            if strategy not in ("refuse", "markers"):
                raise BoardError("on_conflict must be 'refuse' or 'markers'")
            board_paths = self._board_path_subset(paths)
            if board_paths is None:
                with self.sc.connect() as db:
                    _, _, attached = self.sc.body(db, pub_id)
                    board_paths = [f["path"] for f in attached]
            decision = self.sc.apply(
                self.actor, pub_id, local_hashes(board_paths, self.workspace_path),
                paths=board_paths if paths else None,
            )
            if not decision.get("applied"):
                if strategy == "markers":
                    return self._apply_with_markers(pub_id, decision, board_paths)
                return err_result(self._render_conflict(pub_id, decision))
            granted = decision.get("files", [])
            written: List[str] = []
            try:
                written = atomic_write_files(granted, self.workspace_path)
            except BoardError as error:
                self.sc.apply_error(self.actor, pub_id, f"{type(error).__name__}: {error}")
                self.sc.apply_result(self.actor, pub_id, written, False)
                return err_result(
                    f"The board granted the apply but writing failed: {error}. "
                    f"Partial writes were rolled back ({len(written)} files kept)."
                )
            self.sc.apply_result(self.actor, pub_id, written, True)
            lines = [f"Applied {len(written)} file(s) from author `{decision.get('author')}`:"]
            lines += [f"- `{p}`" for p in written]
            kept = [p for p, d in decision.get("decisions", {}).items() if d in ("kept", "unchanged")]
            if kept:
                lines.append(f"Already current, left untouched: {', '.join(f'`{p}`' for p in kept)}")
            return ok_result("\n".join(lines))

        else:
            raise BoardError(f"Unknown tool: {name}")

    # ---- apply / inspect rendering ---------------------------------------

    def _board_path_subset(self, paths: Any) -> Optional[List[str]]:
        if paths is None:
            return None
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise BoardError("paths must be a non-empty list of file path strings")
        return [locate(p, self.workspace_path)[0] for p in paths]

    def _render_expanded(self, rendered: Dict[str, Any]) -> str:
        lines = [f"### Files attached by `{rendered.get('author')}` to `{rendered.get('id')}`:"]
        total = 0
        for entry in rendered.get("files", []):
            shown = entry.get("shown", "full")
            header = (
                f"\n--- {entry.get('path')} ({entry.get('bytes', 0):,} bytes, "
                f"{entry.get('change', 'unknown')}; shown "
                f"{'in full' if shown == 'full' else 'as a diff against the baseline'})"
            )
            if entry.get("original_error"):
                header += f"\nBaseline unavailable: {entry['original_error']}"
            body = entry.get("text", "")
            if not body and entry.get("change") == "unchanged":
                body = "(no differences from the baseline)"
            budget = MAX_INSPECT_CHARS - total
            if len(body) > budget:
                body = body[:budget] + "\n... [truncated: output cap reached]"
                lines.append(header)
                lines.append(body)
                lines.append("\n[Further files omitted: output cap reached. Re-run with `paths` to page.]")
                break
            lines.append(header)
            lines.append(body if body.endswith("\n") or not body else body + "\n")
            total += len(body)
        return "\n".join(lines)

    def _apply_with_markers(self, pub_id: str, decision: Dict[str, Any],
                            board_paths: List[str]) -> Dict[str, Any]:
        """Write granted files plus git-style conflict blocks for the rest.

        The caller explicitly opted in via ``on_conflict='markers'``. The
        prior ``apply`` audit event already records which paths were
        conflicts; the success recorded here reflects that the merge
        markers — not silent overwrites — were written.
        """
        with self.sc.connect() as db:
            author, _, attached = self.sc.body(db, pub_id)
            by_path = {f["path"]: f for f in attached}
        entries, marked = [], []
        for path in board_paths:
            verdict = decision.get("decisions", {}).get(path)
            if verdict in ("write",):
                entries.append(next(
                    f for f in decision.get("files", []) if f["path"] == path))
            elif verdict == "conflict":
                incoming = by_path[path]["content"]
                local_target = self.workspace_path / path
                try:
                    local = (local_target.read_text(encoding="utf-8")
                             if local_target.is_file() else "(absent in workspace)\n")
                except OSError:
                    local = "(unreadable in workspace)\n"
                if not local.endswith("\n"):
                    local += "\n"
                text = (f"<<<<<<< LOCAL (workspace)\n{local}"
                        f"=======\n{incoming}"
                        f"{'' if incoming.endswith(chr(10)) else chr(10)}"
                        f">>>>>>> PEER {author} {pub_id[:8]}\n")
                mode = "x" if (local_target.is_file() and os.access(local_target, os.X_OK)) else "-"
                entries.append({"path": path, "content": text, "mode": mode})
                marked.append(path)
        written: List[str] = []
        try:
            written = atomic_write_files(entries, self.workspace_path)
        except BoardError as error:
            self.sc.apply_error(self.actor, pub_id, f"{type(error).__name__}: {error}")
            self.sc.apply_result(self.actor, pub_id, written, False)
            return err_result(
                f"Marker merge failed while writing: {error}. "
                f"Partial writes were rolled back ({len(written)} files kept)."
            )
        self.sc.apply_result(self.actor, pub_id, written, True)
        lines = [f"Merged {len(written)} file(s) from author `{author}` with conflict markers:"]
        lines += [f"- `{p}`" for p in written]
        if marked:
            lines.append("Resolve the `<<<<<<<` / `>>>>>>>` blocks, then publish your reconciled version.")
        return ok_result("\n".join(lines))

    @staticmethod
    def _render_conflict(pub_id: str, decision: Dict[str, Any]) -> str:
        lines = [
            f"Apply of `{pub_id}` refused: {len(decision.get('decisions', {}))} file(s) need local reconciliation. Nothing was written.",
            "Inspect the peer version with delm_inspect_files, reconcile locally, then retry — or retry with on_conflict='markers' to get git-style conflict blocks.",
        ]
        for path, verdict in decision.get("decisions", {}).items():
            if verdict == "conflict":
                detail = (decision.get("conflicts", {}) or {}).get(path, {})
                reason = detail.get("reason", "local edits require reconciliation")
                lines.append(f"- `{path}`: CONFLICT ({reason})")
            else:
                lines.append(f"- `{path}`: {verdict}")
        return "\n".join(lines)

    # ---- JSON-RPC protocol -------------------------------------------------

    def handle_request(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC request object; None for notifications."""
        if not isinstance(message, dict):
            return self._error(None, JSONRPC_INVALID_REQUEST, "Request must be an object")
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self._error(msg_id, JSONRPC_INVALID_PARAMS, "params must be an object")
        is_notification = "id" not in message

        try:
            if method == "__parse_error__":
                return self._error(msg_id, JSONRPC_PARSE_ERROR, "Parse error")

            if method == "initialize":
                client_version = params.get("protocolVersion")
                version = (
                    client_version
                    if client_version in SUPPORTED_PROTOCOL_VERSIONS
                    else DEFAULT_PROTOCOL_VERSION
                )
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {},
                            "prompts": {},
                        },
                        "serverInfo": {"name": "delm-mcp-server", "version": __version__},
                    },
                }

            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None

            elif method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": self.get_tool_definitions()},
                }

            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments", {})
                if not isinstance(name, str) or not name:
                    return self._error(msg_id, JSONRPC_INVALID_PARAMS, "Missing tool name")
                if not isinstance(args, dict):
                    return self._error(
                        msg_id, JSONRPC_INVALID_PARAMS, "Tool arguments must be an object"
                    )
                if not any(t["name"] == name for t in self.get_tool_definitions()):
                    return self._error(
                        msg_id, JSONRPC_INVALID_PARAMS, f"Unknown tool: {name}"
                    )
                return {"jsonrpc": "2.0", "id": msg_id, "result": self.call_tool(name, args)}

            elif method in ("resources/list", "prompts/list"):
                key = "resources" if method.startswith("resources") else "prompts"
                return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}

            elif method == "resources/templates/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"resourceTemplates": []}}

            elif method == "ping":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

            else:
                if is_notification:
                    log.debug("Ignoring unknown notification: %s", method)
                    return None
                return self._error(
                    msg_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}"
                )
        except Exception as error:  # pragma: no cover - defensive
            log.exception("Error handling method %s", method)
            if is_notification:
                return None
            return self._error(
                msg_id, JSONRPC_INTERNAL_ERROR, f"Internal error: {error}"
            )

    @staticmethod
    def _error(msg_id: Any, code: int, text: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    def handle_message(self, message: Any) -> Optional[Any]:
        """Handle a parsed JSON-RPC message; batch arrays return response lists."""
        if isinstance(message, list):
            responses = []
            for item in message:
                response = self.handle_request(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        return self.handle_request(message)

    # ---- stdio transport ---------------------------------------------------

    def run_stdio(self) -> None:
        """Serve JSON-RPC over stdin/stdout until stdin closes."""
        log.info(
            "Server started for actor '%s' (board=%s workspace=%s)",
            self.actor, self.board_path, self.workspace_path,
        )
        stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
        while True:
            try:
                line = stdin.readline()
            except (OSError, ValueError):
                break
            if not line:
                log.info("stdin closed, shutting down")
                break
            framed = line.startswith(b"Content-Length:")
            try:
                if framed:
                    message = self._read_framed(stdin, line)
                    if message is None:
                        continue
                else:
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        self._write(stdout, self._error(None, JSONRPC_PARSE_ERROR, "Parse error"), False)
                        continue
            except UnicodeDecodeError:
                self._write(stdout, self._error(None, JSONRPC_PARSE_ERROR, "Request is not valid UTF-8"), False)
                continue
            try:
                response = self.handle_message(message)
            except Exception:  # pragma: no cover - defensive
                log.exception("Unhandled error while handling message")
                response = self._error(
                    message.get("id") if isinstance(message, dict) else None,
                    JSONRPC_INTERNAL_ERROR, "Internal error",
                )
            if response is not None:
                try:
                    self._write(stdout, response, framed)
                except (OSError, ValueError, BrokenPipeError):
                    log.info("stdout closed, shutting down")
                    break

    def _read_framed(self, stdin: Any, first_line: bytes) -> Optional[Any]:
        """Read one Content-Length framed JSON-RPC message."""
        try:
            length = int(first_line.decode("ascii").split(":", 1)[1].strip())
        except (ValueError, UnicodeDecodeError):
            log.warning("Ignoring malformed Content-Length header")
            return None
        while True:
            header = stdin.readline()
            if not header or header.strip() in (b"", b"\r\n"):
                break
        if length < 0 or length > 64 * 1024 * 1024:
            log.warning("Ignoring framed message with invalid length %s", length)
            return None
        raw = b""
        while len(raw) < length:
            chunk = stdin.read(length - len(raw))
            if not chunk:
                log.warning("Truncated framed message (got %d of %d bytes)", len(raw), length)
                return None
            raw += chunk
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"jsonrpc": "2.0", "id": None, "method": "__parse_error__"}

    @staticmethod
    def _write(stdout: Any, response: Any, framed: bool) -> None:
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        if framed:
            stdout.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        stdout.write(payload)
        if not framed:
            stdout.write(b"\n")
        stdout.flush()
