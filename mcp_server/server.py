"""DeLM MCP Server implementation using stdio JSON-RPC.

Requires only the Python standard library. Interacts with board.shared_context.SharedContext
to coordinate agents through decentralized task queues, status, notes (FACT/FAIL),
and diff sharing.
"""

import difflib
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from board.shared_context import BoardError, SharedContext


def format_view(view: Dict[str, Any]) -> str:
    """Format a board view into a readable markdown summary for the agent."""
    lines = [f"### DeLM Shared Board (seq: {view.get('sequence', 0)})"]
    
    # Active / Changed Queue
    queue = view.get("queue", [])
    lines.append(f"\n**Task Queue ({len(queue)} items):**")
    if not queue:
        lines.append("- (No tasks on the board)")
    else:
        for item in queue:
            status_indicator = "✅ [DONE]" if item.get("state") == "done" else (
                f"⏳ [CLAIMED by {item.get('owner')}]" if item.get("state") == "claimed" else "⚪ [OPEN]"
            )
            files_info = f" ({len(item.get('files', []))} files attached)" if item.get("files") else ""
            lines.append(f"- **`{item.get('id', '')[:8]}`** {status_indicator} **{item.get('subject', 'Untitled')}**{files_info}")
            if item.get("detail"):
                lines.append(f"  *Detail:* {item.get('detail')}")
            if item.get("summary"):
                lines.append(f"  *Summary:* {item.get('summary')}")

    # Notes (FACT / FAIL)
    notes = view.get("notes", [])
    if notes:
        lines.append(f"\n**Recent Discoveries / Notes ({len(notes)}):**")
        for note in notes:
            note_type = note.get("type", "NOTE")
            icon = "💡 [FACT]" if note_type == "FACT" else "⚠️ [FAIL]"
            author = note.get("actor", "unknown")
            files_info = f" (attached: {', '.join(f.get('path', '') for f in note.get('files', []))})" if note.get("files") else ""
            lines.append(f"- {icon} **{author}** on `{note.get('scope', 'general')}`: {note.get('text', '')}{files_info}")
            if note.get("basis"):
                lines.append(f"  *Evidence/Basis:* {note.get('basis')}")

    # Status updates
    statuses = view.get("status", [])
    if statuses:
        lines.append(f"\n**Agent Working States:**")
        for s in statuses:
            lines.append(f"- **{s.get('actor')}**: {s.get('text')}")

    return "\n".join(lines)


class DelmMcpServer:
    """Standard Model Context Protocol (MCP) stdio server backed by DeLM SharedContext."""

    def __init__(self, actor: str, board_path: Path, workspace_path: Path, peers: Optional[List[str]] = None):
        self.actor = actor
        self.board_path = Path(board_path).resolve()
        self.workspace_path = Path(workspace_path).resolve()
        self.peers = list(peers) if peers else [actor, "peer-1", "peer-2", "peer-3", "codex", "claude", "oc", "agy"]
        if actor not in self.peers:
            self.peers.append(actor)

        # Initialize shared context with dynamic actors registration
        self.sc = self._create_shared_context()

    def _create_shared_context(self) -> SharedContext:
        """Create or load the SharedContext instance."""
        self.board_path.parent.mkdir(parents=True, exist_ok=True)
        # Check if database already exists with registered actors
        return SharedContext(str(self.board_path), self.peers, original=self._get_original_file)

    def _get_original_file(self, path: str) -> Optional[bytes]:
        """Fetch the base/original task file from workspace if present, or None if new."""
        target = self.workspace_path / path
        if target.is_file():
            try:
                return target.read_bytes()
            except Exception:
                return None
        return None

    def _read_attached_files(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Read files relative to workspace or /tmp for attachment."""
        files = []
        for p in paths:
            file_path = Path(p)
            if not file_path.is_absolute():
                full_path = (self.workspace_path / file_path).resolve()
            else:
                full_path = file_path
            
            if not full_path.is_file():
                raise BoardError(f"File to attach not found: {p}")
            
            try:
                content = full_path.read_text(encoding="utf-8")
            except Exception as e:
                raise BoardError(f"Could not read text file {p}: {e}")
            
            mode = "x" if os.access(full_path, os.X_OK) else "-"
            rel_path = str(file_path if not file_path.is_absolute() else full_path)
            files.append(dict(path=rel_path, content=content, mode=mode))
        return files

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return the MCP tool schemas exposed to the LLM agent."""
        return [
            {
                "name": "delm_read_board",
                "description": "Read the shared DeLM coordination board. Inspects claimed tasks, open tasks, peer notes (FACT/FAIL), and agent states. Can optionally wait for peer activity.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "wait_seconds": {
                            "type": "number",
                            "description": "Seconds to wait (up to 25s) if no new events exist. 0 for immediate non-blocking read.",
                            "default": 0
                        },
                        "full": {
                            "type": "boolean",
                            "description": "Whether to return the full board instead of incremental changes since the last read.",
                            "default": False
                        }
                    }
                }
            },
            {
                "name": "delm_reserve_task",
                "description": "Atomically declare intent, create a scope, and claim an item so other agents avoid duplicate work.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "Short title of the scope/task (e.g., 'Implement database migration runner')"
                        },
                        "detail": {
                            "type": "string",
                            "description": "Inputs, outputs, expected interface, and dependency requirements."
                        }
                    },
                    "required": ["subject", "detail"]
                }
            },
            {
                "name": "delm_claim_task",
                "description": "Claim an existing open task from the board by ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ID of the open task to claim"
                        }
                    },
                    "required": ["item_id"]
                }
            },
            {
                "name": "delm_complete_task",
                "description": "Mark a claimed task as done, publish a solution summary, and optionally attach modified workspace files.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "The item ID you previously claimed"
                        },
                        "summary": {
                            "type": "string",
                            "description": "Summary of results, changed functions, limitation notes, and test outcomes."
                        },
                        "attach_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of relative workspace file paths to attach/publish to the board."
                        }
                    },
                    "required": ["item_id", "summary"]
                }
            },
            {
                "name": "delm_publish_note",
                "description": "Publish a verified finding (FACT) or disproven approach/blocker (FAIL) to the shared context so peer agents learn immediately.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "note_type": {
                            "type": "string",
                            "enum": ["FACT", "FAIL"],
                            "description": "FACT for verified observation/measurement, FAIL for contradicted hypothesis."
                        },
                        "text": {
                            "type": "string",
                            "description": "The finding or blocker description (target ~400 chars)"
                        },
                        "scope": {
                            "type": "string",
                            "description": "Component or area impacted (target ~120 chars)"
                        },
                        "basis": {
                            "type": "string",
                            "description": "Reproducible evidence, commands run, or sample measurements (target ~200 chars)"
                        },
                        "attach_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of files (e.g. reproduction scripts, benchmark logs) to attach."
                        }
                    },
                    "required": ["note_type", "text", "scope", "basis"]
                }
            },
            {
                "name": "delm_update_status",
                "description": "Update your current working status on the board (e.g. 'Investigating race condition in auth.py').",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Brief description of what you are currently investigating or doing"
                        }
                    },
                    "required": ["text"]
                }
            },
            {
                "name": "delm_inspect_files",
                "description": "Inspect file diffs or contents published by peer agents behind a task completion or note.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "publication_id": {
                            "type": "string",
                            "description": "Publication ID or item ID containing attachments"
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of specific file paths to view in full"
                        }
                    },
                    "required": ["publication_id"]
                }
            },
            {
                "name": "delm_apply_files",
                "description": "Apply and write files published by a peer into your local workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "publication_id": {
                            "type": "string",
                            "description": "Publication ID containing the files to pull"
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of file paths to apply. If omitted, applies all files in the publication."
                        }
                    },
                    "required": ["publication_id"]
                }
            }
        ]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool call and return content blocks."""
        try:
            if name == "delm_read_board":
                wait_sec = float(arguments.get("wait_seconds", 0))
                full = bool(arguments.get("full", False))
                view = self.sc.read(self.actor, full=full, wait_seconds=wait_sec)
                text = format_view(view)
                return {"content": [{"type": "text", "text": text}], "isError": False}

            elif name == "delm_reserve_task":
                subject = arguments["subject"]
                detail = arguments["detail"]
                res = self.sc.reserve(self.actor, subject, detail)
                status_str = "Won claim" if res.get("won") else f"Already owned by {res.get('owner')}"
                msg = f"{status_str} on task `{res.get('item')}`: **{subject}**\n\n{format_view(res.get('view', {}))}"
                return {"content": [{"type": "text", "text": msg}], "isError": False}

            elif name == "delm_claim_task":
                item_id = arguments["item_id"]
                res = self.sc.claim(self.actor, item_id)
                status_str = "Claimed successfully" if res.get("won") else f"Could not claim (owned by {res.get('owner')})"
                msg = f"{status_str} for item `{item_id}`.\n\n{format_view(res.get('view', {}))}"
                return {"content": [{"type": "text", "text": msg}], "isError": not res.get("won", False)}

            elif name == "delm_complete_task":
                item_id = arguments["item_id"]
                summary = arguments["summary"]
                attach_paths = arguments.get("attach_paths", [])
                files = self._read_attached_files(attach_paths) if attach_paths else None
                res = self.sc.done(self.actor, item_id, summary, files=files)
                msg = (f"Completed task `{item_id}`. Publication ID: `{res.get('publication_id')}`\n"
                       f"Attached {len(attach_paths)} files.\n\n{format_view(res.get('view', {}))}")
                return {"content": [{"type": "text", "text": msg}], "isError": False}

            elif name == "delm_publish_note":
                note_type = arguments["note_type"]
                text = arguments["text"]
                scope = arguments["scope"]
                basis = arguments["basis"]
                attach_paths = arguments.get("attach_paths", [])
                files = self._read_attached_files(attach_paths) if attach_paths else None
                res = self.sc.note(self.actor, note_type, text, scope, basis, files=files)
                msg = (f"Published {note_type} `{res.get('publication_id')}` on scope '{scope}'.\n"
                       f"Attached {len(attach_paths)} files.\n\n{format_view(res.get('view', {}))}")
                return {"content": [{"type": "text", "text": msg}], "isError": False}

            elif name == "delm_update_status":
                text = arguments["text"]
                res = self.sc.status(self.actor, text)
                msg = f"Status updated to: '{text}'.\n\n{format_view(res.get('view', {}))}"
                return {"content": [{"type": "text", "text": msg}], "isError": False}

            elif name == "delm_inspect_files":
                pub_id = arguments["publication_id"]
                paths = arguments.get("paths")
                rendered = self.sc.expand(self.actor, pub_id, paths=paths)
                lines = [f"### Expanded files for publication `{pub_id}`:"]
                for f in rendered:
                    lines.append(f"\n#### File: `{f['path']}` ({f['change']}, {f['bytes']} bytes)")
                    lines.append("```diff\n" + f.get("diff", "") + "\n```")
                return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}

            elif name == "delm_apply_files":
                pub_id = arguments["publication_id"]
                paths = arguments.get("paths")
                
                # Fetch files from bodydb via shared_context
                with self.sc.connect() as db:
                    author, _, files = self.sc.body(db, pub_id)
                    selected = self.sc.select(files, paths)
                
                applied = []
                for f in selected:
                    target = self.workspace_path / f["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f["content"], encoding="utf-8")
                    if f.get("mode") == "x":
                        os.chmod(target, os.stat(target).st_mode | 0o111)
                    applied.append(f["path"])

                msg = f"Successfully applied {len(applied)} file(s) from author `{author}`:\n" + "\n".join(f"- `{p}`" for p in applied)
                return {"content": [{"type": "text", "text": msg}], "isError": False}

            else:
                return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

        except BoardError as e:
            return {"content": [{"type": "text", "text": f"DeLM Board Error: {str(e)}"}], "isError": True}
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"Unexpected Error: {str(e)}"}], "isError": True}

    def handle_request(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle single JSON-RPC MCP request."""
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "delm-mcp-server",
                        "version": "0.1.0"
                    }
                }
            }

        elif method == "notifications/initialized":
            return None

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": self.get_tool_definitions()
                }
            }

        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            result = self.call_tool(name, args)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result
            }

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}"
                }
            }

    def run_stdio(self):
        """Standard IO JSON-RPC event loop."""
        sys.stderr.write(f"[DeLM MCP] Server started for actor '{self.actor}'. Workspace: {self.workspace_path}\n")
        sys.stderr.flush()

        stdin_buffer = sys.stdin.buffer

        while True:
            line = stdin_buffer.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            # Standard Content-Length header framing or raw JSON per-line
            if line_str.startswith("Content-Length:"):
                try:
                    length = int(line_str.split(":", 1)[1].strip())
                    # Read empty line
                    stdin_buffer.readline()
                    raw_body = stdin_buffer.read(length)
                    message = json.loads(raw_body.decode("utf-8"))
                except Exception as e:
                    sys.stderr.write(f"[DeLM MCP] Framing error: {e}\n")
                    continue
            else:
                try:
                    message = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

            response = self.handle_request(message)
            if response is not None:
                resp_bytes = json.dumps(response, ensure_ascii=False).encode("utf-8")
                # Write standard framed response or line response
                if line_str.startswith("Content-Length:"):
                    sys.stdout.buffer.write(f"Content-Length: {len(resp_bytes)}\r\n\r\n".encode("utf-8"))
                    sys.stdout.buffer.write(resp_bytes)
                else:
                    sys.stdout.buffer.write(resp_bytes + b"\n")
                sys.stdout.buffer.flush()
