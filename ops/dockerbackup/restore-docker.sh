#!/usr/bin/env bash
# ==============================================================================
# restore-docker.sh - 本機 Docker 服務災害復原異機還原腳本 (全自動環境自檢、重建與下載版)
# ==============================================================================
set -euo pipefail

BACKUP_ROOT="${1:-}"
FORCE_TRADING="${2:-}"

# 1. 若當前目錄就是備份資料夾（內含 projects_and_mounts.tar.zst），優先以當前目錄還原
if [ -z "$BACKUP_ROOT" ] && [ -f "$PWD/projects_and_mounts.tar.zst" ]; then
  BACKUP_ROOT="$PWD"
  echo "偵測到當前目錄即為備份資料夾: $BACKUP_ROOT"
fi

# 2. 若參數為 "latest" 或未指定參數時：優先抓取本機 /home/roytai/backups 最新目錄，無本機時 fallback 抓 Google Drive
if [ -z "$BACKUP_ROOT" ] || [ "$BACKUP_ROOT" == "latest" ]; then
  LATEST_LOCAL=$(find /home/roytai/backups -mindepth 1 -maxdepth 1 -type d -name "docker-*" 2>/dev/null | sort | tail -n 1 || true)
  if [ -n "$LATEST_LOCAL" ] && [ -d "$LATEST_LOCAL" ]; then
    BACKUP_ROOT="$LATEST_LOCAL"
    echo "自動選擇最新本機備份: $BACKUP_ROOT"
  elif command -v rclone &>/dev/null && rclone listremotes 2>/dev/null | grep -q "^GoogleDrive:"; then
    LATEST_REMOTE=$(rclone lsf "GoogleDrive:ServerBackups" --dirs-only 2>/dev/null | sort | tail -n 1 | tr -d '/' || true)
    if [ -n "$LATEST_REMOTE" ]; then
      BACKUP_ROOT="GoogleDrive:ServerBackups/$LATEST_REMOTE"
      echo "本機無備份，自動選擇最新 Google Drive 遠端備份: $BACKUP_ROOT"
    fi
  fi
fi

CLEANUP_TEMP_DIR=""
if [[ "$BACKUP_ROOT" == *":"* ]]; then
  echo "偵測到雲端遠端路徑 ($BACKUP_ROOT)，正在透過 rclone 下載至本機暫存目錄..."
  CLEANUP_TEMP_DIR=$(mktemp -d /tmp/dr-restore-XXXXXX)
  rclone copy -L "$BACKUP_ROOT" "$CLEANUP_TEMP_DIR" --transfers 4 --fast-list
  BACKUP_ROOT="$CLEANUP_TEMP_DIR"
  echo "遠端備份下載完成！"
fi

if [ -z "$BACKUP_ROOT" ] || [ ! -d "$BACKUP_ROOT" ]; then
  echo "使用方式: $0 <備份目錄路徑 | latest | GoogleDrive:ServerBackups/目錄名> [--force-trading]" >&2
  echo "範例 1 (還原最新備份): $0 latest" >&2
  echo "範例 2 (自訂本機路徑): $0 /home/roytai/backups/docker-20261022_120000" >&2
  echo "範例 3 (直接自雲端抓取): $0 GoogleDrive:ServerBackups/docker-20261022_120000" >&2
  exit 1
fi

DB_DIR="$BACKUP_ROOT/db-dumps"
SQLITE_ROOT="$DB_DIR/sqlite-root"
HOST_DIR="$BACKUP_ROOT/host-configs"
ARCHIVE_FILE="$BACKUP_ROOT/projects_and_mounts.tar.zst"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"

echo "=================================================="
echo "  開始執行 Docker 服務災害復原還原"
echo "  來源備份目錄: $BACKUP_ROOT"
echo "=================================================="

SUDO=""
if [ "$EUID" -ne 0 ]; then
  SUDO="sudo"
fi

# ------------------------------------------------------------------------------
# 0. 異機前置環境自檢與自動修復 (Pre-Flight Checks & Auto-Setup)
# ------------------------------------------------------------------------------
echo "[0/6] 執行異機環境前置自檢與自動配置..."

# 0.1 檢查使用者帳號與 UID/GID
CURRENT_USER=$(id -un)
CURRENT_UID=$(id -u)
CURRENT_GID=$(id -g)
if [ "$CURRENT_USER" != "roytai" ] || [ "$CURRENT_UID" -ne 1000 ] || [ "$CURRENT_GID" -ne 1000 ]; then
  echo "  [警告] 目前執行身分為 ${CURRENT_USER} (${CURRENT_UID}:${CURRENT_GID})，非預期之 roytai (1000:1000)。"
  echo "         若您是直接使用 sudo 執行本腳本，建議改以 roytai 帳號直接執行 (腳本內部需要時會自動呼叫 sudo)。"
else
  echo "  - [OK] 使用者帳號與 UID/GID 檢查通過: roytai (1000:1000)"
fi

# 0.2 自動檢查並安裝缺少的系統基礎套件 (Debian/Ubuntu)
MISSING_PKGS=()
command -v curl &>/dev/null    || MISSING_PKGS+=("curl")
command -v git &>/dev/null     || MISSING_PKGS+=("git")
command -v zstd &>/dev/null    || MISSING_PKGS+=("zstd")
command -v sqlite3 &>/dev/null || MISSING_PKGS+=("sqlite3")
command -v rclone &>/dev/null  || MISSING_PKGS+=("rclone")
command -v pactl &>/dev/null   || MISSING_PKGS+=("pulseaudio-utils")
if command -v dpkg &>/dev/null; then
  for pkg in pipewire-pulse wireplumber bluez hfsprogs; do
    if ! dpkg -s "$pkg" &>/dev/null; then
      MISSING_PKGS+=("$pkg")
    fi
  done
fi

if [ "${#MISSING_PKGS[@]}" -gt 0 ] && command -v apt-get &>/dev/null; then
  echo "  - 偵測到新機缺少基礎套件 (${MISSING_PKGS[*]})，正在自動安裝..."
  $SUDO apt-get update -qq
  $SUDO apt-get install -y "${MISSING_PKGS[@]}"
fi

for cmd in docker tar zstd curl git find; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "錯誤: 找不到必要指令 $cmd，請先安裝 Docker 與基礎工具。" >&2
    exit 1
  fi
done

# 0.3 檢查 Docker 群組權限與決定 DOCKER 執行命令
DOCKER="docker"
if ! docker info &>/dev/null; then
  echo "  - 目前帳號尚無直接操作 Docker socket 權限，自動將 roytai 加入 docker 群組..."
  $SUDO usermod -aG docker roytai 2>/dev/null || true
  DOCKER="$SUDO docker"
  echo "  - 本次執行自動切換使用 '$DOCKER' (重新登入後即可免 sudo 執行 docker)。"
else
  echo "  - [OK] Docker 權限與 Daemon 連線正常。"
fi

# 0.4 自動啟用 systemd loginctl linger (確保無頭模式下 /run/user/1000/pulse 常駐)
if [ ! -f "/var/lib/systemd/linger/roytai" ] && command -v loginctl &>/dev/null; then
  echo "  - 自動啟用 loginctl enable-linger roytai (確保背景音訊服務常駐)..."
  $SUDO loginctl enable-linger roytai || true
else
  echo "  - [OK] systemd linger 常駐已啟用。"
fi

# ------------------------------------------------------------------------------
# 1. 還原 Docker Daemon 設定
# ------------------------------------------------------------------------------
echo "[1/6] 檢查並還原 Docker Daemon 配置..."
if [ -f "$HOST_DIR/daemon.json" ]; then
  if ! cmp -s "$HOST_DIR/daemon.json" /etc/docker/daemon.json 2>/dev/null; then
    echo "  - 還原 /etc/docker/daemon.json (配置自訂 DNS 8.8.8.8/1.1.1.1 與 Mirror)..."
    $SUDO mkdir -p /etc/docker
    $SUDO cp "$HOST_DIR/daemon.json" /etc/docker/daemon.json
    echo "  - 正在重啟 Docker 服務使設定生效..."
    $SUDO systemctl restart docker
  else
    echo "  - [OK] /etc/docker/daemon.json 已是最新配置，無須重啟 Docker。"
  fi
else
  echo "  - 無備份之 daemon.json，維持目標機器現有設定。"
fi

# ------------------------------------------------------------------------------
REPO_DIR=/home/roytai/xiaozhi
MONOREPO_COMMIT="$(awk -F': ' '$1 == "XiaoZhi Monorepo Commit" {print $2; exit}' "$BACKUP_ROOT/manifest.txt" 2>/dev/null || true)"
if [ -z "$MONOREPO_COMMIT" ]; then
  echo "錯誤: 備份 manifest 缺少 XiaoZhi monorepo commit。" >&2
  exit 1
fi
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone https://github.com/myrest/xiaozhi-Home.git "$REPO_DIR"
fi
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  echo "錯誤: $REPO_DIR 有未提交修改；為避免覆蓋，停止還原。" >&2
  exit 1
fi
git -C "$REPO_DIR" fetch --quiet origin main
git -C "$REPO_DIR" checkout --quiet --detach "$MONOREPO_COMMIT"

# 2. 還原持久資料並預建必要之宿主機掛載點
# ------------------------------------------------------------------------------
echo "[2/6] 正在還原持久資料與 Bind-Mount 狀態至原路徑..."
if [ -f "$ARCHIVE_FILE" ]; then
  zstd -dc "$ARCHIVE_FILE" | $SUDO tar --numeric-owner -xpf - -C /
  echo "  - 解壓縮完成。"
else
  echo "錯誤: 找不到封裝檔 $ARCHIVE_FILE" >&2
  exit 1
fi

echo "  - 檢查並補齊宿主機音訊 Cookie、CF 卡掛載與外部目錄權限..."
mkdir -p \
  /home/roytai/.config/pulse \
  /home/roytai/.cache/receptron-laya \
  /home/roytai/TEST/laya-flow-builder/data \
  /home/roytai/xiaozhi/state/server/data \
  /home/roytai/xiaozhi/state/server/uploadfile \
  /home/roytai/xiaozhi/state/mysql/data \
  /home/roytai/xiaozhi/state/mcp/endpoint \
  /home/roytai/xiaozhi/state/mcp/endpoint-urls \
  /home/roytai/xiaozhi/state/mcp/playback \
  /home/roytai/xiaozhi/state/redis \
  /home/roytai/xiaozhi/state/models/SenseVoiceSmall \
  /home/roytai/xiaozhi/state/secrets \
  /home/roytai/EvenniaMud \
  /home/roytai/StorySystem \
  /home/roytai/cf-integration
$SUDO chown 1000:1000 /home/roytai/.cache/receptron-laya /home/roytai/TEST/laya-flow-builder/data 2>/dev/null || true
if [ ! -f "/home/roytai/.config/pulse/cookie" ]; then
  dd if=/dev/urandom of=/home/roytai/.config/pulse/cookie bs=256 count=1 status=none
  chmod 600 /home/roytai/.config/pulse/cookie
  $SUDO chown 1000:1000 /home/roytai/.config/pulse/cookie
fi

if [ ! -d "/run/user/1000/pulse" ]; then
  echo "  - 嘗試啟動使用者級 pipewire-pulse 服務..."
  systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || $SUDO mkdir -p /run/user/1000/pulse
fi

# 自動檢查 CF 卡 (UUID=ba896486-bc49-379c-8f44-b82d7a85bed5) 是否已插上新機並自動掛載
CF_UUID="ba896486-bc49-379c-8f44-b82d7a85bed5"
if ! mountpoint -q /mnt/cfcard 2>/dev/null; then
  if $SUDO blkid -U "$CF_UUID" &>/dev/null; then
    echo "  - 偵測到實體 CF 卡 ($CF_UUID)，正在自動掛載至 /mnt/cfcard..."
    $SUDO mkdir -p /mnt/cfcard
    $SUDO mount -t hfsplus -o ro,uid=1000,gid=1000,umask=000 UUID="$CF_UUID" /mnt/cfcard || true
  else
    echo "  - [提示] 目前未偵測到實體 CF 卡 ($CF_UUID)，已預建 /mnt/cfcard 空目錄供容器掛載。"
    $SUDO mkdir -p /mnt/cfcard
  fi
else
  echo "  - [OK] /mnt/cfcard 已正確掛載。"
fi

# ------------------------------------------------------------------------------
# 3. 還原乾淨一致的 SQLite 資料庫
# ------------------------------------------------------------------------------
echo "[3/6] 還原 SQLite 資料庫快照 (並清理殘留之 WAL/SHM)..."
if [ -d "$SQLITE_ROOT" ]; then
  while IFS= read -r -d '' -u 3 backup_db; do
    rel_path="${backup_db#"$SQLITE_ROOT/"}"
    target_db="/$rel_path"
    $SUDO mkdir -p "$(dirname "$target_db")"
    $SUDO cp -f "$backup_db" "$target_db"
    $SUDO rm -f "${target_db}-wal" "${target_db}-shm"
    echo "  - 已還原: $target_db"
  done 3< <(find "$SQLITE_ROOT" -type f -print0)
$SUDO chown -R 1000:1000 /home/roytai/.hermes /home/roytai/xiaozhi/state/mcp 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 4. 下載核對模型並啟動統一的 XiaoZhi Compose stack
# ------------------------------------------------------------------------------
echo "[4/6] 啟動 monorepo Compose stack..."
MODEL_PT="/home/roytai/xiaozhi/state/models/SenseVoiceSmall/model.pt"
MODEL_SHA256="833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea"
if [ -f "$MODEL_PT" ]; then
  if ! printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PT" | sha256sum -c - >/dev/null 2>&1; then
    echo "錯誤: 現有 SenseVoiceSmall 權重 SHA-256 不符。" >&2
    exit 1
  fi
else
  MODEL_TMP="${MODEL_PT}.tmp"
  MODEL_OK=0
  for MODEL_URL in \
    "https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt" \
    "https://huggingface.co/FunAudioLLM/SenseVoiceSmall/resolve/main/model.pt"; do
    if curl -fsSL --retry 3 "$MODEL_URL" -o "$MODEL_TMP" \
      && printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_TMP" | sha256sum -c - >/dev/null 2>&1; then
      mv -f "$MODEL_TMP" "$MODEL_PT"
      MODEL_OK=1
      break
    fi
    rm -f "$MODEL_TMP"
  done
  if [ "$MODEL_OK" -ne 1 ]; then
    echo "錯誤: 下載的 SenseVoiceSmall 權重與已驗證 SHA-256 不符。" >&2
    exit 1
  fi
fi

cd /home/roytai/xiaozhi
$DOCKER compose -f deploy/compose.yaml config --quiet
SQL_DUMP="$DB_DIR/mysql_xiaozhi_databases.sql.zst"
USER_DUMP="$DB_DIR/mysql_xiaozhi_users.sql"
if [ ! -f "$SQL_DUMP" ] || ! zstd -t "$SQL_DUMP" >/dev/null || [ ! -s "$USER_DUMP" ]; then
  echo "錯誤: 找不到或無法驗證 XiaoZhi MySQL dump/account grants。" >&2
  exit 1
fi

echo "    建置固定版本映像，先啟動空白 MySQL..."
$DOCKER compose -f deploy/compose.yaml build
$DOCKER compose -f deploy/compose.yaml up -d xiaozhi-esp32-server-db

echo "    等待 MySQL 健康檢查就緒 (最長 90 秒)..."
HEALTH="starting"
for i in {1..45}; do
  HEALTH=$($DOCKER inspect xiaozhi-esp32-server-db --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
  if [ "$HEALTH" == "healthy" ]; then
    echo "    MySQL 已健康就緒！"
    break
  fi
  sleep 2
done
if [ "$HEALTH" != "healthy" ]; then
  echo "錯誤: MySQL 未在期限內就緒。" >&2
  exit 1
fi

TABLE_COUNT=$(printf '%s\n' "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema IN ('xiaozhi_esp32_server','voiceprint_db');" | $DOCKER exec -i xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -s')
if [ "${TABLE_COUNT:-0}" -ne 0 ]; then
  echo "錯誤: XiaoZhi/voiceprint 資料庫非空；拒絕覆寫還原目標。" >&2
  exit 1
fi
echo "    匯入 XiaoZhi 與 voiceprint logical dump/account grants..."
zstd -dc "$SQL_DUMP" | $DOCKER exec -i xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot'
$DOCKER exec -i xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot' < "$USER_DUMP"
echo "    MySQL 匯入完成，啟動完整 Compose stack..."
$DOCKER compose -f deploy/compose.yaml up -d

# Step 4.3 & 4.4: 啟動交易服務前，自動探測舊機 (192.168.0.152) 是否仍在區網運行，防止實盤雙開下單！
LOCAL_IPS=$(hostname -I 2>/dev/null || true)
OLD_HOST_TRADING_ALIVE=0
if [[ " $LOCAL_IPS " != *" 192.168.0.152 "* ]]; then
  if curl -s -m 2 http://192.168.0.152:17001 &>/dev/null || curl -s -m 2 http://192.168.0.152:17003 &>/dev/null; then
    OLD_HOST_TRADING_ALIVE=1
  fi
fi

if [ "$OLD_HOST_TRADING_ALIVE" -eq 1 ] && [ "$FORCE_TRADING" != "--force-trading" ]; then
  echo "  ⚠️ [嚴重安全攔截] 偵測到舊主機 (192.168.0.152) 的交易機器人 (埠 17001/17003) 仍在區網運行中！"
  echo "     為防止新舊兩台機器同時使用同一組 API Key 雙開下單，已自動暫停啟動 aiTrade 與 GradTrad。"
  echo "     請先關閉舊機容器，再手動啟動或加上 --force-trading 參數重跑。"
else
  echo "  - [C] 啟動 aiTrade (自動建置 agent-trader 映像，啟動 BNB 實盤)..."
  cd /home/roytai/aiTrade
  $DOCKER compose up -d --build BNB

  echo "  - [D] 啟動 GradTrad (自動建置 gradtrad-live-multi 映像)..."
  cd /home/roytai/GradTrad
  $DOCKER compose up -d --build
fi

# Step 4.5: hermes-agent
echo "  - [E] 啟動 hermes-agent (自動 clone 官方最新版本並套用本地 override)..."
if [ ! -d "/home/roytai/hermes-agent/.git" ]; then
  echo "    正在拉取官方最新 nousresearch/hermes-agent 代碼..."
  TMP_OVERRIDE=""
  if [ -f "/home/roytai/hermes-agent/docker-compose.override.yml" ]; then
    TMP_OVERRIDE=$(mktemp)
    cp -a /home/roytai/hermes-agent/docker-compose.override.yml "$TMP_OVERRIDE"
  fi
  rm -rf /home/roytai/hermes-agent
  git clone https://github.com/nousresearch/hermes-agent.git /home/roytai/hermes-agent
  if [ -n "$TMP_OVERRIDE" ] && [ -f "$TMP_OVERRIDE" ]; then
    mv -f "$TMP_OVERRIDE" /home/roytai/hermes-agent/docker-compose.override.yml
  fi
fi
cd /home/roytai/hermes-agent
HERMES_UID=1000 HERMES_GID=1000 $DOCKER compose up -d --build

# Step 4.6: laya-flow-builder (自動拉取 myrest/laya 與 myrest/laya-flow-builder 並還原 data/)
echo "  - [F] 啟動 laya-flow-builder (自動拉取 GitHub 原始碼並套用備份之 data/，首次啟動將自 HuggingFace 下載模型至 ~/.cache/receptron-laya)..."
mkdir -p /home/roytai/TEST
if [ ! -d "/home/roytai/TEST/laya/.git" ]; then
  echo "    正在拉取 myrest/laya (fix/multilingual-special-tokens 分支)..."
  rm -rf /home/roytai/TEST/laya
  git clone -b fix/multilingual-special-tokens https://github.com/myrest/laya.git /home/roytai/TEST/laya
fi
if [ ! -d "/home/roytai/TEST/laya-flow-builder/.git" ]; then
  echo "    正在拉取 myrest/laya-flow-builder (main 分支)..."
  TMP_LAYA_DATA=""
  if [ -d "/home/roytai/TEST/laya-flow-builder/data" ]; then
    TMP_LAYA_DATA=$(mktemp -d)
    cp -a /home/roytai/TEST/laya-flow-builder/data/. "$TMP_LAYA_DATA/"
  fi
  rm -rf /home/roytai/TEST/laya-flow-builder
  git clone https://github.com/myrest/laya-flow-builder.git /home/roytai/TEST/laya-flow-builder
  mkdir -p /home/roytai/TEST/laya-flow-builder/data
  if [ -n "$TMP_LAYA_DATA" ] && [ -d "$TMP_LAYA_DATA" ]; then
    cp -a "$TMP_LAYA_DATA/." /home/roytai/TEST/laya-flow-builder/data/
    rm -rf "$TMP_LAYA_DATA"
  fi
fi
$SUDO chown -R 1000:1000 /home/roytai/TEST/laya-flow-builder/data /home/roytai/.cache/receptron-laya 2>/dev/null || true
cd /home/roytai/TEST/laya-flow-builder
$DOCKER compose up -d --build

# ------------------------------------------------------------------------------
# 5. 還原宿主機服務設定 (Systemd User Units 與 Cloudflare Tunnel)
# ------------------------------------------------------------------------------
echo "[5/6] 還原使用者級 Systemd Units 並檢查宿主機服務..."
if [ -d "$HOST_DIR/systemd-user" ]; then
  mkdir -p /home/roytai/.config/systemd/user
  cp -a "$HOST_DIR/systemd-user/." /home/roytai/.config/systemd/user/
  systemctl --user daemon-reload 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 6. 自動化健康與硬體環境診斷報告 (Automated Post-Restore Diagnostics)
# ------------------------------------------------------------------------------
echo "[6/6] 執行還原後自動化環境診斷..."
echo "--------------------------------------------------"
$DOCKER ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo "--------------------------------------------------"

# 6.1 檢查小智 OTA 介面連通性
if curl -fsS -m 5 http://127.0.0.1:16001/xiaozhi/ota/ &>/dev/null; then
  echo "✅ [檢查通過] 小智 OTA / Web 智控台介面 (http://127.0.0.1:16001/xiaozhi/ota/) 回應正常。"
else
  echo "⏳ [啟動中] 小智 Web 智控台尚未回應 (Java 啟動約需 20~40 秒，請稍後以 docker logs xiaozhi-esp32-server-web 確認)。"
fi

# 6.1b 檢查 Laya Flow Builder 介面連通性
if curl -fsS -m 5 http://127.0.0.1:15000/health &>/dev/null; then
  echo "✅ [檢查通過] Laya Flow Builder 服務 (http://127.0.0.1:15000/health) 回應正常。"
else
  echo "⏳ [啟動中] Laya Flow Builder 尚未回應 (首次啟動需自 HuggingFace 下載約 1.3GB ONNX 模型，最長約 15 分鐘，請以 docker logs -f laya-flow-builder-laya-1 查看進度)。"
fi

# 6.2 檢查藍芽喇叭 Sink 是否已連線
EXPECTED_SINK="bluez_output.88_FA_42_42_97_F7.1"
if command -v pactl &>/dev/null; then
  if pactl list short sinks 2>/dev/null | grep -q "$EXPECTED_SINK"; then
    echo "✅ [檢查通過] 藍芽喇叭 ($EXPECTED_SINK) 已連線就緒。"
  else
    echo "⚠️ [需人工處理] 尚未在 PipeWire 偵測到藍芽喇叭 ($EXPECTED_SINK)。"
    echo "   目前可用音訊 Sinks："
    pactl list short sinks 2>/dev/null | sed 's/^/     /' || true
    echo "   👉 請參考同目錄下之 restore-note.md 進行 bluetoothctl 配對。"
  fi
fi

# 6.3 檢查 Cloudflare Tunnel 狀態
if systemctl is-active --quiet cloudflared 2>/dev/null; then
  echo "✅ [檢查通過] cloudflared.service 已在宿主機運行。"
elif [ -f "$HOST_DIR/cloudflared.service" ]; then
  echo "⚠️ [需人工處理] 宿主機尚未啟用 cloudflared.service。"
  echo "   👉 確認舊機已關閉 cloudflared 後，請執行："
  echo "      sudo cp $HOST_DIR/cloudflared.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared"
fi

echo ""
echo "詳細人工收尾說明：$REPO_DIR/ops/dockerbackup/restore-note.md"
if [ -n "${CLEANUP_TEMP_DIR:-}" ] && [ -d "$CLEANUP_TEMP_DIR" ]; then
  echo "清理遠端暫存還原目錄: $CLEANUP_TEMP_DIR"
  rm -rf "$CLEANUP_TEMP_DIR"
fi
