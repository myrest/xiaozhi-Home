#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/deploy/compose.yaml"
ENDPOINT_DIR="$ROOT_DIR/state/mcp/endpoint"
URL_DIR="$ROOT_DIR/state/mcp/endpoint-urls"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Docker Compose v2 is required.' >&2
    exit 1
fi

mkdir -p "$ENDPOINT_DIR" "$URL_DIR"
chmod 700 "$ROOT_DIR/state/mcp" "$ENDPOINT_DIR" "$URL_DIR"
if [ ! -s "$ENDPOINT_DIR/.mcp-endpoint-server.cfg" ] \
    || [ ! -s "$URL_DIR/tool_endpoint" ] \
    || [ ! -s "$URL_DIR/xiaozhi_endpoint" ] \
    || [ ! -s "$URL_DIR/health_url" ]; then
    docker compose -f "$COMPOSE_FILE" --profile bootstrap run --build --rm --no-deps endpoint-init
fi

docker compose -f "$COMPOSE_FILE" config --quiet
docker compose -f "$COMPOSE_FILE" up -d --build
