#!/usr/bin/env sh
set -eu

umask 077

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/deploy/compose.yaml"
ENDPOINT_DIR="$ROOT_DIR/state/mcp/endpoint"
URL_DIR="$ROOT_DIR/state/mcp/endpoint-urls"
REMINDER_DIR="$ROOT_DIR/state/mcp/reminders"
TOKEN_FILE="$ROOT_DIR/state/mcp/reminder_token"
PLAYBACK_DIR="$ROOT_DIR/state/mcp/playback"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Docker Compose v2 is required.' >&2
    exit 1
fi

DEPLOY_UID=$(id -u)
if [ "$DEPLOY_UID" != 0 ] && [ "$DEPLOY_UID" != 1000 ]; then
    printf '%s\n' 'Run deployment as UID 1000 or root; MCP state is private to UID 1000.' >&2
    exit 1
fi

mkdir -p "$ENDPOINT_DIR" "$URL_DIR" "$REMINDER_DIR" "$PLAYBACK_DIR"
if [ "$DEPLOY_UID" = 0 ]; then
    chown 1000:1000 "$ROOT_DIR/state/mcp" "$ENDPOINT_DIR" "$URL_DIR" "$REMINDER_DIR" "$PLAYBACK_DIR"
fi
chmod 700 "$ROOT_DIR/state/mcp" "$ENDPOINT_DIR" "$URL_DIR" "$REMINDER_DIR" "$PLAYBACK_DIR"
if [ ! -s "$ENDPOINT_DIR/.mcp-endpoint-server.cfg" ] \
    || [ ! -s "$URL_DIR/tool_endpoint" ] \
    || [ ! -s "$URL_DIR/xiaozhi_endpoint" ] \
    || [ ! -s "$URL_DIR/health_url" ] \
    || [ ! -f "$TOKEN_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" --profile bootstrap run --build --rm --no-deps endpoint-init
fi
if [ "$DEPLOY_UID" = 0 ]; then
    chown 1000:1000 "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

docker compose -f "$COMPOSE_FILE" config --quiet
docker compose -f "$COMPOSE_FILE" up -d --build
