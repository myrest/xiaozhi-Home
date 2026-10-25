# XiaoZhi Home

此 Git repository 是 XiaoZhi 主機的公開 monorepo；所有程式碼與客製修改都會推送到 `myrest/xiaozhi-Home`。**不可提交 runtime secrets、使用者資料或模型權重。**實際資料放在 Git 忽略的 `state/`；Google Drive 備份依目前選擇不做客戶端加密，必須限制 Drive 帳號存取。

## 目錄

```text
services/server/       XiaoZhi Python server、plugins、設定範本與測試
services/manager/      manager-api / manager-web；上游版本與 SHA 見 UPSTREAM.md
services/mcp/          bedtime MCP endpoint、playback、鎖定的 Python dependencies
  ├── endpoint/        upstream patch 與 credential bootstrap
  └── bedtime_mcp/     MCP tools 與故事播放

deploy/                統一 Docker Compose 與各服務 Dockerfile
scripts/deploy.sh      建置並啟動整個 XiaoZhi stack
docs/server-operations.md XiaoZhi 端點、裝置設定與日常維護
state/                 Git 忽略；所有 XiaoZhi runtime state 與 secrets
```

`state/` 的主要資料：`server/data/`、`server/uploadfile/`、`mysql/data/`、`mcp/{endpoint,endpoint-urls,playback}/`、`redis/`、`models/`、`secrets/`。MySQL 與 manager 的環境檔只放在 `state/secrets/`，權限設為 `0600`；永遠不要把檔案內容貼到公開 issue、commit 或 log。

## 建置與操作

```bash
# 驗證 Compose 並重建/啟動全部服務
./scripts/deploy.sh

# 狀態、log、停止
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs -f xiaozhi-esp32-server
docker compose -f deploy/compose.yaml down
```

MCP 回歸測試以鎖定的 endpoint/playback images 執行：`./services/mcp/scripts/run_tests.sh`。

`./scripts/deploy.sh` 會在 MCP endpoint credential 檔案缺少時自動初始化；也可在 endpoint state 尚未初始化時手動執行：

```bash
docker compose -f deploy/compose.yaml --profile bootstrap run --build --rm --no-deps endpoint-init
```

單一 Compose project 管理 server、manager、MySQL、Redis、voiceprint 與 bedtime MCP。播放服務只接 `mcp-backend` internal network；endpoint 同時透過 `bedtime-mcp-endpoint` alias 提供給 XiaoZhi server。故事媒體仍由宿主機 `/mnt` read-only、`rslave` 掛載；`/mnt/cfcard` 不搬入 repo，並由獨立備份保護。

主機對外連接埠維持現有裝置與管理端點：語音 WebSocket `16000`、管理台 `16001`、視覺分析 `8003`。MySQL、Redis、聲紋與 MCP 僅在 Docker network 內提供服務。

設備設定使用 OTA `http://<host>:16001/xiaozhi/ota/`（保留結尾斜線）；直接 WebSocket 為 `ws://<host>:16000/xiaozhi/v1/`。視覺分析路徑是 `/mcp/vision/explain`。

Dockerfile 的 base image 使用 digest；manager 使用 `npm ci` 與 Maven；MCP 使用已鎖定的 pip dependencies。Server Python dependencies 隨鎖定的 upstream server base image 提供。更新上游 server 時，`sync-upstream.sh` 只會改 `services/server/`，不會覆蓋 monorepo 其他模組。執行前先提交 server subtree 變更，再檢查同步差異。

## 備份與還原

```bash
~/bin/dockerbackup/backup-docker.sh
~/bin/dockerbackup/restore-docker.sh latest
```

備份／還原工具與說明只放在主機本地 `~/bin/dockerbackup/`，不納入此 repo；備份輸出位於 `/home/roytai/backups/`，每日 03:00 cron 呼叫主機本地備份程式。備份會將 `~/bin/` 納入 archive，讓工具隨主機資料復原；Manifest 記錄 monorepo commit，XiaoZhi 原始碼仍由公開 Git remote 還原。Archive 包含 `state/`、MySQL logical dump、SQLite online snapshots 與宿主機設定；不包含 Git 可取得的 XiaoZhi 原始碼、MySQL raw datadir 或 SenseVoiceSmall 權重。

備份包含 credentials 與使用者資料，Google Drive remote 目前不做客戶端加密；Drive 權限即為資料保護邊界。CF 卡媒體由使用者既有的另一套備份保護。SenseVoiceSmall 還原時下載並核對 SHA-256；無網路時 ASR 模型不能還原。

## 客製內容與來源

- Server：Gemini schema/timeout 修正、Edge TTS 修正、自訂 `play_story` MCP plugin，以及本地 MCP endpoint 設定來源。
- Manager：Agent/Intent 相容性修改與 `llmCapabilities` 前端修改；來源基準為 `services/manager/UPSTREAM.md` 所記錄的官方 commit，不推送到官方 upstream。
- MCP：Bedtime 故事目錄、播放計畫、播放狀態工具與 token bootstrap。
