#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-main}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$PROJECT_ROOT/services/server"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

if [ ! -d "$SERVER_DIR" ]; then
  printf 'Server source tree not found: %s\n' "$SERVER_DIR" >&2
  exit 1
fi

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain -- services/server)" ]; then
  printf '%s\n' 'Server subtree has uncommitted changes; commit or stash them before upstream sync.' >&2
  git -C "$PROJECT_ROOT" status --short -- services/server
  exit 1
fi

URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/refs/heads/${TARGET}.tar.gz"
if [[ "$TARGET" =~ ^v[0-9] ]]; then
  URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/refs/tags/${TARGET}.tar.gz"
fi

if ! curl -fsSL "$URL" -o "$TEMP_DIR/upstream.tar.gz"; then
  URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/${TARGET}.tar.gz"
  curl -fsSL "$URL" -o "$TEMP_DIR/upstream.tar.gz"
fi

mkdir -p "$TEMP_DIR/extracted"
tar -xzf "$TEMP_DIR/upstream.tar.gz" -C "$TEMP_DIR/extracted"
EXTRACTED_ROOT="$(find "$TEMP_DIR/extracted" -mindepth 1 -maxdepth 1 -type d -print -quit)"
SOURCE_DIR="$EXTRACTED_ROOT/main/xiaozhi-server"
if [ ! -d "$SOURCE_DIR" ]; then
  printf '%s\n' 'Downloaded archive has no main/xiaozhi-server subtree.' >&2
  exit 1
fi

rsync -a --delete \
  --exclude='.git' \
  --exclude='.gitignore' \
  --exclude='data/' \
  --exclude='tmp/' \
  --exclude='models/SenseVoiceSmall/model.pt' \
  --exclude='music/' \
  --exclude='plugins_func/functions/*' \
  "$SOURCE_DIR/" "$SERVER_DIR/"

printf 'Upstream %s synchronized into services/server. Review and test before committing.\n' "$TARGET"
git -C "$PROJECT_ROOT" status --short -- services/server
