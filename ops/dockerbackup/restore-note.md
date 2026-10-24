# Docker 主機災害復原說明

## 備份格式與安全邊界

- 每日 03:00 由 user crontab 執行 `~/xiaozhi/ops/dockerbackup/backup-docker.sh`；本機保留 7 天，Google Drive 保留 30 天。
- 備份包括 monorepo commit SHA、`~/xiaozhi/state`、`xiaozhi_esp32_server` 與 `voiceprint_db` logical dump、`voiceprint_api` 建帳／權限 SQL、SQLite online snapshots 與既有宿主機／其他服務資料。Git 可取得的 XiaoZhi source、MySQL raw datadir、可重下載的 SenseVoiceSmall 權重不放入 archive。
- Google Drive remote **沒有 client-side encryption**。備份含 credentials、資料庫與主機設定；Drive 帳號／資料夾存取權即為保護邊界。不要公開分享備份目錄。
- `~/xiaozhi/state/secrets/` 的資料不進 Git，但會進備份以支援還原。不要把內容貼到 log、issue 或 commit。
- `/mnt/cfcard` 的故事媒體不在 `dockerbackup` 範圍；已由使用者的獨立備份保護。SenseVoiceSmall 在還原時從 ModelScope/Hugging Face 下載，並核對 SHA-256 `833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea`。
- 新版還原腳本只處理 monorepo 的新備份格式；既有舊 archive 保留至原 retention 到期，並保留各自內附的舊還原腳本。

## 還原

先在新主機安裝 Docker Compose v2、Git、rclone、zstd、sqlite3、curl 與必要的硬體套件；確定公開 repo 可存取、Google Drive remote `GoogleDrive:` 已設定，並已接上 CF 卡。執行：

```bash
cd ~/xiaozhi
./ops/dockerbackup/restore-docker.sh latest
```

也可指定本機 archive 或 `GoogleDrive:ServerBackups/<backup-directory>`。腳本從 manifest 取 commit SHA、clone/checkout 公開 monorepo、還原 state，先建置映像並啟動 MySQL，將 XiaoZhi logical dump 匯入空資料庫後才啟動完整 Compose stack；若資料庫非空便停止，避免覆寫或誤用舊資料。

還原會啟動主機備份中列出的其他服務。啟動交易服務前，先確認舊主機的交易容器與 Cloudflare Tunnel 已停用；腳本會在區網偵測到舊交易服務時攔截，但無法偵測其他網段的主機。

## XiaoZhi 與實體媒體

- Compose 位於 `~/xiaozhi/deploy/compose.yaml`；所有 XiaoZhi source 位於 `~/xiaozhi/services/`，runtime state 位於 Git 忽略的 `~/xiaozhi/state/`。
- MySQL root 與 manager datasource 密碼由 `state/secrets/{mysql,manager}.env` 提供。新主機請還原備份中的 secret file，不要在 Compose 或 Git 放明文密碼。
- CF 卡 UUID `ba896486-bc49-379c-8f44-b82d7a85bed5` 唯讀掛載於 `/mnt/cfcard`。還原腳本會嘗試自動掛載；失敗時確認讀卡機及 `/etc/fstab`，容器以 `/mnt` read-only、`rslave` 掛載。
- 新主機需重新配對藍牙喇叭。原喇叭 MAC 為 `88:FA:42:42:97:F7`：
  ```bash
  bluetoothctl
  # 進入互動介面後執行：
  scan on
  pair 88:FA:42:42:97:F7
  trust 88:FA:42:42:97:F7
  connect 88:FA:42:42:97:F7
  exit
  pactl list short sinks
  ```
  若更換喇叭，使用 `scan on` 找到新 MAC，並依 `pactl list short sinks` 確認主機音訊輸出。
- 啟用備份中的 Cloudflare Tunnel 前，先確認舊主機已停止 Tunnel，並確認 origin 指向新主機：管理台／OTA `16001`、語音 WebSocket `16000`；視覺分析才需要 `8003`。

## 其他主機服務

交易、Hermes、Laya Flow Builder、Cloudflare Tunnel 與 PipeWire 的其餘還原順序仍由 `restore-docker.sh` 處理。Laya 的 `admin-token`、`mcp-token` 與決策資料由備份還原至 `~/TEST/laya-flow-builder/data/`；ONNX 模型快取不進備份，首次啟動會重新下載。新機器使用者應為 `roytai`（UID/GID 1000）；還原後依腳本診斷確認容器、OTA/Web 管理台、外部網路與硬體狀態。
