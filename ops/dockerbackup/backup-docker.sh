#!/usr/bin/env bash
set -euo pipefail
BACKUP_ROOT="${1:-/home/roytai/backups/docker-$(date +%Y%m%d_%H%M%S)}"
DB_DIR="$BACKUP_ROOT/db-dumps"
SQLITE_ROOT="$DB_DIR/sqlite-root"
HOST_DIR="$BACKUP_ROOT/host-configs"
ARCHIVE_FILE="$BACKUP_ROOT/projects_and_mounts.tar.zst"
MANIFEST_FILE="$BACKUP_ROOT/manifest.txt"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"
echo "開始執行 Docker 備份：$BACKUP_ROOT"
for cmd in docker tar zstd sqlite3 find git; do
  command -v "$cmd" >/dev/null || { echo "錯誤: 找不到必要指令 $cmd" >&2; exit 1; }
done
REPO_DIR="/home/roytai/xiaozhi"
if ! git -C "$REPO_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "錯誤: XiaoZhi monorepo 不存在或沒有 commit；備份中止。" >&2
  exit 1
fi
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  echo "錯誤: XiaoZhi monorepo 有未提交變更；先 commit/push 再備份。" >&2
  exit 1
fi
MONOREPO_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
mkdir -p "$DB_DIR" "$SQLITE_ROOT" "$HOST_DIR"
chmod 700 "$BACKUP_ROOT"
{
  echo "Backup Timestamp: $(date -Iseconds)"
  echo "Host: $(hostname)"
  echo "Kernel: $(uname -r)"
  echo "Docker Version: $(docker --version)"
  echo "XiaoZhi Monorepo Commit: $MONOREPO_COMMIT"
  echo "=== RUNNING CONTAINERS ==="
  docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
} > "$MANIFEST_FILE"
if ! docker ps --format '{{.Names}}' | grep -q '^xiaozhi-esp32-server-db$'; then
  echo "錯誤: MySQL 容器 xiaozhi-esp32-server-db 未運行，備份中止。" >&2
  exit 1
fi
if ! docker exec xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD missing}" exec mysqldump -uroot --set-gtid-purged=OFF --databases xiaozhi_esp32_server voiceprint_db --single-transaction --routines --triggers --events' | zstd -T0 > "$DB_DIR/mysql_xiaozhi_databases.sql.zst"; then
  echo "錯誤: MySQL dump 失敗。" >&2
  exit 1
fi
if ! USER_CREATE_SQL="$(printf "%s\n" "SHOW CREATE USER 'voiceprint_api'@'%';" | docker exec -i xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD missing}" exec mysql -uroot --batch --raw --skip-column-names')" \
  || ! GRANTS_SQL="$(printf "%s\n" "SHOW GRANTS FOR 'voiceprint_api'@'%';" | docker exec -i xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD missing}" exec mysql -uroot --batch --raw --skip-column-names')"; then
  echo "錯誤: MySQL service account dump 失敗。" >&2
  exit 1
fi
{
  printf '%s;\n' "$USER_CREATE_SQL"
  while IFS= read -r statement; do
    [ -n "$statement" ] && printf '%s;\n' "$statement"
  done <<< "$GRANTS_SQL"
  printf '%s\n' 'FLUSH PRIVILEGES;'
} > "$DB_DIR/mysql_xiaozhi_users.sql"
chmod 600 "$DB_DIR/mysql_xiaozhi_databases.sql.zst" "$DB_DIR/mysql_xiaozhi_users.sql"
REQUIRED_SQLITE_DATABASES=(
  /home/roytai/xiaozhi/state/server/data/powermem_dev.db
  /home/roytai/xiaozhi/state/mcp/playback/playback.sqlite3
)
for db_file in "${REQUIRED_SQLITE_DATABASES[@]}"; do
  if [ ! -f "$db_file" ]; then
    echo "錯誤: 缺少必須備份的 SQLite 資料庫 $db_file" >&2
    exit 1
  fi
done
while IFS= read -r -d '' -u 3 db_file; do
  rel_path="${db_file#/}"
  dest_file="$SQLITE_ROOT/$rel_path"
  mkdir -p "$(dirname "$dest_file")"
  if sqlite3 "$db_file" ".backup '$dest_file'" </dev/null 2>/dev/null \
    && [ "$(sqlite3 "$dest_file" 'PRAGMA quick_check;' 2>/dev/null)" = "ok" ]; then
    echo "[OK] $db_file"
  else
    rm -f "$dest_file"
    case "$db_file" in
      /home/roytai/xiaozhi/state/server/data/powermem_dev.db|/home/roytai/xiaozhi/state/mcp/playback/playback.sqlite3)
        echo "錯誤: 必須備份的 SQLite 資料庫無法建立或驗證快照: $db_file" >&2
        exit 1
        ;;
      *)
        echo "[略過非標準 SQLite] $db_file"
        ;;
    esac
  fi
done 3< <(find /home/roytai/xiaozhi/state /home/roytai/.hermes -not -path '*/.git*' -not -path '*/.codegraph*' -not -path '*/node_modules*' -not -path '*/.venv*' -not -path '*/voiceprint-modelscope-cache*' -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print0 2>/dev/null)
if [ -r /etc/docker/daemon.json ]; then cp /etc/docker/daemon.json "$HOST_DIR/daemon.json"; fi
if [ -r /etc/systemd/system/cloudflared.service ]; then cp /etc/systemd/system/cloudflared.service "$HOST_DIR/cloudflared.service"; fi
if [ -r /etc/fstab ]; then cp /etc/fstab "$HOST_DIR/fstab.bak"; fi
if [ -d /home/roytai/.config/systemd/user ]; then cp -a /home/roytai/.config/systemd/user "$HOST_DIR/systemd-user"; fi
TAR_TARGETS=()
for path in /home/roytai/xiaozhi/state /home/roytai/aiTrade /home/roytai/aiTradeHome /home/roytai/GradTrad /home/roytai/GradTradHome /home/roytai/TEST/laya-flow-builder/data /home/roytai/hermes-agent/docker-compose.override.yml /home/roytai/.hermes /home/roytai/bin /home/roytai/.ssh /home/roytai/.gitconfig /home/roytai/.wrangler /home/roytai/.config/pulse/cookie; do
  [ -e "$path" ] && TAR_TARGETS+=("$path")
done
(
  # Archive as container root so private runtime files remain restorable.
  # GNU tar returns 1 when live files change during the snapshot.
  if docker run --rm --network none \
    --mount type=bind,src=/home/roytai,dst=/home/roytai,readonly \
    --entrypoint /usr/bin/tar \
    mysql@sha256:9d48c42f8341068f199116dfccb919b607c99765b5c61e549a548a43033471a4 \
    --warning=no-file-ignored --warning=no-file-changed --warning=no-file-removed --numeric-owner -cpf - \
      --exclude='*.sock' \
      --exclude='*/.git' \
      --exclude='*/.venv' \
      --exclude='*/node_modules' \
      --exclude='*/.npm' \
      --exclude='*/.codegraph' \
      --exclude='/home/roytai/.wrangler/logs' \
      --exclude='/home/roytai/.wrangler/registry' \
      --exclude='*/__pycache__' \
      --exclude='*/.pytest_cache' \
      --exclude='*/.cache' \
      --exclude='/home/roytai/.hermes/cache' \
      --exclude='*/state/models/SenseVoiceSmall/model.pt' \
      --exclude='*/state/mysql/data' \
      --exclude='*/state/server/data/powermem_dev.db*' \
      --exclude='*/state/mcp/playback/playback.sqlite3*' \
      "${TAR_TARGETS[@]}"; then
    :
  else
    tar_status=$?
    [ "$tar_status" -eq 1 ]
  fi
) | zstd -T0 > "$ARCHIVE_FILE"
if [ ! -s "$DB_DIR/mysql_xiaozhi_users.sql" ] \
  || ! zstd -t "$DB_DIR/mysql_xiaozhi_databases.sql.zst" >/dev/null \
  || ! zstd -t "$ARCHIVE_FILE" >/dev/null; then
  echo "錯誤: 備份檔案驗證失敗；停止上傳。" >&2
  exit 1
fi
ls -lh "$BACKUP_ROOT" "$DB_DIR"
echo "本機備份大小: $(du -sh "$BACKUP_ROOT" | cut -f1)"
REMOTE_TARGET="GoogleDrive:ServerBackups/$(basename "$BACKUP_ROOT")"
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q '^GoogleDrive:'; then
  rclone copy "$BACKUP_ROOT" "$REMOTE_TARGET" --progress
  rclone delete "GoogleDrive:ServerBackups" --min-age 30d --rmdirs
fi
LOCAL_PARENT_DIR="$(dirname "$BACKUP_ROOT")"
if [ -d "$LOCAL_PARENT_DIR" ] && [[ "$LOCAL_PARENT_DIR" == *"/backups"* || "$LOCAL_PARENT_DIR" == *"/backup"* ]]; then find "$LOCAL_PARENT_DIR" -maxdepth 1 -type d -name 'docker-*' -mtime +7 -exec rm -rf {} +; fi
echo "備份完成時間: $(date)"
