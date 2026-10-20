# DeLM MCP server image.
#
# Zero third-party dependencies: the server runs on the Python standard
# library only. Build from the repository root:
#
#   docker build -f mcp_server/Dockerfile -t delm-mcp:1.0.0 .
#
# Each agent runs its own container attached to the SAME board file (bind
# mount or shared volume) with its own --actor and --workspace:
#
#   docker run -i --rm \
#     -v /shared/project/.delm:/data \
#     -v /shared/project/work-codex:/work \
#     delm-mcp:1.0.0 \
#     --actor codex-1 --board /data/board.sqlite --workspace /work
#
# The container speaks MCP over stdin/stdout (`-i`), so point your client
# at `docker run -i ...` (see docs/MCP_SETUP_GUIDE.md).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY mcp_server/ ./mcp_server/
COPY board/ ./board/
COPY pyproject.toml README.md ./

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 delm \
    && mkdir -p /data /work \
    && chown delm:delm /data /work

USER delm

VOLUME ["/data"]

ENTRYPOINT ["delm-mcp"]
CMD ["--help"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=10s \
    CMD ["delm-mcp", "--version"]
