#!/bin/sh
set -eu

MCP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
REPO_ROOT=$(CDPATH= cd -- "$MCP_DIR/../.." && pwd)

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker is required to run the pinned MCP test environments.' >&2
    exit 1
fi

docker build -f "$REPO_ROOT/services/mcp/Dockerfile.endpoint" -t xiaozhi-mcp-endpoint:custom-v2 "$REPO_ROOT"
docker build -f "$REPO_ROOT/services/mcp/Dockerfile" -t xiaozhi-mcp-playback:custom-v2 "$REPO_ROOT"

docker run --rm --workdir /workspace/mcp \
    --volume "$MCP_DIR:/workspace/mcp:ro" \
    --entrypoint python \
    xiaozhi-mcp-playback:custom-v2 \
    -m unittest discover -s tests -v

docker run --rm --workdir /workspace/mcp \
    --volume "$MCP_DIR:/workspace/mcp:ro" \
    --entrypoint python \
    xiaozhi-mcp-endpoint:custom-v2 \
    -m unittest discover -s tests -p 'test_bootstrap.py' -v
