# xiaozhi-Home

本專案為小智 AI 音箱（xiaozhi-esp32）的客製化語音服務核心代碼，掛載於 Docker 容器 `xiaozhi-esp32-server`。

- **上游開源專案**：[`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server)（對齊子目錄 `main/xiaozhi-server`）
- **本地自訂遠端倉庫**：[`myrest/xiaozhi-Home`](https://github.com/myrest/xiaozhi-Home)
- **容器掛載目標**：`~/xiaozhi-Home/core` 與 `~/xiaozhi-Home/plugins_func` 分別熱掛載至容器內 `/opt/xiaozhi-esp32-server/`

---

## 本地客製修補與特性 (Custom Patches)

1. **Gemini LLM 相容性修正** (`core/providers/llm/gemini/gemini.py`)：
   - 解決新版 `google-generativeai` SDK 的 Function Declaration Schema 欄位檢查嚴格限制，自動遞迴清理不支援的 JSON schema 屬性。
   - 修正超時參數調用為 `request_options={"timeout": ...}`。
2. **Edge-TTS 2026 舊端點 403 智慧防護** (`core/providers/tts/edge.py`)：
   - 微軟於 2026-10 棄用舊端點 (`speech.platform.bing.com`)。
   - 內建**動態版本守衛**：若容器環境內的 `edge-tts < 7.3.0`，自動切換至新端點 `api.msedgeservices.com`；若未來 Docker 映像檔升級至 `edge-tts >= 7.3.1`，則自動讓位使用原生端點，具備向前相容性。
3. **自訂 Python 工具外掛** (`plugins_func/`)：
   - 放置自編的工具外掛程式碼，支援熱載入，重啟主服務即可生效。

---

## 版本控制架構與工作流程 (Version Control & Workflow)

為避免官方 Monorepo（Java/Vue/文檔）3,000+ 個頻繁 Commit 污染個人倉庫，本專案採用 **Vendor Snapshot（壓平同步）** 模式維護：

```text
[官方 GitHub Monorepo]
  └─ main/xiaozhi-server/  ──(./sync-upstream.sh 壓平抽取)──>  [~/xiaozhi-Home (本地)]
                                                                     │
                                                            (git commit & push)
                                                                     ▼
                                                       [myrest/xiaozhi-Home (個人 Remote)]
```

### 1. 平常開發自訂外掛 (Daily Development Workflow)

當在 `plugins_func/functions/` 增加自撰工具或修改代碼時：

```bash
cd ~/xiaozhi-Home

# 1. 提交至個人 GitHub
git add .
git commit -m "feat: add my custom plugin"
git push origin main

# 2. 重啟音箱容器即時載入生效
docker restart xiaozhi-esp32-server
```

### 2. 同步官方上游新版本 (Upstream Sync Workflow)

當官方發布新版本（例如 `v0.9.8`）或新修復時，執行專案根目錄的無污染同步腳本：

```bash
cd ~/xiaozhi-Home

# 步驟 1：一鍵安全抓取官方新版本（自動保護 plugins_func 與自訂檔案）
./sync-upstream.sh [版本標籤或 main，預設為 main]

# 步驟 2：檢視官方檔案變動
git diff

# 步驟 3：重啟容器測試音箱對話與各模組運作
docker restart xiaozhi-esp32-server

# 步驟 4：確認無誤後，以「單一乾淨 Commit」推進個人遠端
git add .
git commit -m "chore: sync with upstream <版本號>"
git push origin main
```

---

## 目錄結構

```text
xiaozhi-Home/
├── core/                # 語音核心、ASR/TTS/LLM 通訊邏輯（熱掛載）
├── plugins_func/        # 自訂 Python 外掛工具庫（熱掛載）
├── config/              # 配置載入器與音效資源
├── sync-upstream.sh     # 官方版本無污染同步腳本（可執行）
├── Dockerfile-server    # 自編映像檔規格
└── app.py               # 服務主進入點
```
