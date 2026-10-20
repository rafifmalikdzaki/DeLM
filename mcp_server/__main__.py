"""Allow `python -m mcp_server` as an alias for the CLI."""

import sys

from mcp_server.cli import main

if __name__ == "__main__":
    sys.exit(main())
