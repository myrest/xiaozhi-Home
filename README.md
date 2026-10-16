# xiaozhi-Home

本專案為小智 AI 音箱（xiaozhi-esp32）的客製化語音服務核心代碼，掛載於 Docker 容器 `xiaozhi-esp32-server`。

- 上游開源專案來源：[`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server)（對齊 `main/xiaozhi-server`）
- 本地自訂遠端倉庫：[`myrest/xiaozhi-Home`](https://github.com/myrest/xiaozhi-Home)

---

## 本地客製修補與特性

1. **Gemini LLM 修補** (`core/providers/llm/gemini/gemini.py`)：
   - 修正 Google Generative AI 新版 SDK 結構清理與 Function Declaration Schema 欄位相容性。
   - 修正呼叫超時傳參 `request_options={"timeout": ...}`。
2. **Edge-TTS 2026 端點修補** (`core/providers/tts/edge.py`)：
   - 覆蓋微軟 2026 已棄用的舊端點，轉向 `api.msedgeservices.com`。
3. **自訂工具外掛** (`plugins_func/`)：
   - 自撰的 Python 工具與功能擴充存放於此，熱掛載至容器即時生效。

---

## 目錄結構

```text
xiaozhi-Home/
├── core/                # 核心通訊、ASR/TTS/LLM 模組（熱掛載）
├── plugins_func/        # 自訂 Python 外掛工具（熱掛載）
├── config/              # 配置載入器與音效資源
├── sync-upstream.sh     # 官方版本無污染同步腳本
├── Dockerfile-server    # 自編映像檔規格
└── app.py               # 服務主進入點
```

---

## 同步官方上游更新

若官方發布新版本或修復，本專案使用快照壓平同步，**不會將官方數千個 Commit 歷史拉入本倉庫**：

```bash
# 同步官方指定版本（例如 v0.9.8 或 main）
./sync-upstream.sh [版本標籤或 main]

# 檢查變更
git diff

# 確認並測試完成後，以單一 Commit 推進自身遠端
git add .
git commit -m "chore: sync with upstream <版本號>"
git push origin main
```
