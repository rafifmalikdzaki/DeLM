#!/usr/bin/env python3
"""CLI entry point for the DeLM Model Context Protocol (MCP) server."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path

# Allow running directly from a checkout without installation.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server import __version__
from mcp_server.server import DelmMcpServer, log, validate_actor_name

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeLM multi-agent coordination MCP server (stdio JSON-RPC, stdlib only)",
    )
    parser.add_argument(
        "--actor",
        default=os.environ.get("DELM_ACTOR", "agent-1"),
        help="Unique agent identity for this worker (default: $DELM_ACTOR or 'agent-1')",
    )
    parser.add_argument(
        "--board",
        default=os.environ.get("DELM_BOARD_PATH", ".delm/board.sqlite"),
        help="Shared board SQLite file all agents point at (default: $DELM_BOARD_PATH or '.delm/board.sqlite')",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("DELM_WORKSPACE", "."),
        help="Agent workspace root for attach/apply paths (default: $DELM_WORKSPACE or current dir)",
    )
    parser.add_argument(
        "--baseline",
        default=os.environ.get("DELM_BASELINE", ""),
        help="Optional pristine-checkout root used to diff peer files (default: $DELM_BASELINE or none)",
    )
    parser.add_argument(
        "--peers",
        nargs="*",
        default=None,
        help="Expected peer actor names (default: just this actor)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio",),
        default="stdio",
        help="Transport to serve (only 'stdio' is supported)",
    )
    parser.add_argument(
        "--git-exclude",
        action="store_true",
        help="Append .delm/ to the workspace .git/info/exclude so the board never dirties git status",
    )
    parser.add_argument(
        "--log-level",
        choices=VALID_LOG_LEVELS,
        default=os.environ.get("DELM_LOG_LEVEL", "INFO"),
        help="Stderr log verbosity (default: $DELM_LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(asctime)s [delm-mcp] %(levelname)s %(message)s",
    )

    try:
        actor = validate_actor_name(args.actor)
    except ValueError as error:
        print(f"delm-mcp: {error}", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"delm-mcp: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    baseline = Path(args.baseline).resolve() if args.baseline else None
    if baseline is not None and not baseline.is_dir():
        print(f"delm-mcp: baseline is not a directory: {baseline}", file=sys.stderr)
        return 2

    board = Path(args.board).resolve()
    if board.exists() and not board.is_file():
        print(f"delm-mcp: board path is not a regular file: {board}", file=sys.stderr)
        return 2
    try:
        board.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"delm-mcp: cannot create board directory {board.parent}: {error}", file=sys.stderr)
        return 1

    try:
        server = DelmMcpServer(
            actor=actor,
            board_path=board,
            workspace_path=workspace,
            peers=args.peers,
            baseline_path=baseline,
            git_exclude=args.git_exclude,
        )
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"delm-mcp: cannot open board {board}: {error}", file=sys.stderr)
        return 1

    def _on_sigterm(signum: int, _frame: object) -> None:
        log.info("received signal %s, shutting down", signum)
        server.stop()
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (OSError, ValueError, RuntimeError):
        pass

    try:
        server.run_stdio()
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
        server.stop()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
