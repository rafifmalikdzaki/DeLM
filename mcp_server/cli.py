#!/usr/bin/env python3
"""CLI entry point for the DeLM Model Context Protocol (MCP) server."""

import argparse
import os
import sys
from pathlib import Path

# Ensure root of repo is in python path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.server import DelmMcpServer


def main():
    parser = argparse.ArgumentParser(description="DeLM Multi-Agent MCP Server")
    parser.add_argument(
        "--actor",
        default=os.environ.get("DELM_ACTOR", "agent-1"),
        help="Unique agent identity / name for this worker (default: $DELM_ACTOR or 'agent-1')"
    )
    parser.add_argument(
        "--board",
        default=os.environ.get("DELM_BOARD_PATH", ".delm/board.sqlite"),
        help="Path to shared board SQLite file (default: $DELM_BOARD_PATH or '.delm/board.sqlite')"
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("DELM_WORKSPACE", "."),
        help="Path to the agent workspace root (default: $DELM_WORKSPACE or current dir)"
    )
    parser.add_argument(
        "--peers",
        nargs="*",
        default=None,
        help="List of expected peer actor names (optional)"
    )

    args = parser.parse_args()

    board_path = Path(args.board).resolve()
    workspace_path = Path(args.workspace).resolve()

    server = DelmMcpServer(
        actor=args.actor,
        board_path=board_path,
        workspace_path=workspace_path,
        peers=args.peers
    )
    server.run_stdio()


if __name__ == "__main__":
    main()
