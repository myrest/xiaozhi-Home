#!/usr/bin/env bash
# ==============================================================================
# sync-upstream.sh
# 
# 用途：從官方 GitHub (xinnan-tech/xiaozhi-esp32-server) 安全抓取指定版本的
#       xiaozhi-server 源碼並覆蓋本地，完全不引入官方 3,000+ 個 Git Commit 歷史。
#
# 使用方式：
#   ./sync-upstream.sh [版本標籤或分支，例如 v0.9.8 或 main，預設為 main]
# ==============================================================================

set -euo pipefail

TARGET="${1:-main}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "==> 檢查本地 Git 工作目錄狀態..."
cd "$PROJECT_ROOT"
if [ -n "$(git status --porcelain)" ]; then
    echo "❌ 錯誤：工作目錄有未提交的修改，請先 commit 或 stash 後再執行同步。"
    git status -s
    exit 1
fi

echo "==> 正在從官方倉庫下載 target: ${TARGET} ..."
URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/refs/heads/${TARGET}.tar.gz"
if [[ "$TARGET" =~ ^v[0-9] ]]; then
    URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/refs/tags/${TARGET}.tar.gz"
fi

if ! curl -fsSL "$URL" -o "${TEMP_DIR}/upstream.tar.gz"; then
    # 若以 tag 嘗試失敗，嘗試 branch 格式
    URL="https://github.com/xinnan-tech/xiaozhi-esp32-server/archive/${TARGET}.tar.gz"
    curl -fsSL "$URL" -o "${TEMP_DIR}/upstream.tar.gz"
fi

echo "==> 正在解壓縮 main/xiaozhi-server 到臨時目錄..."
mkdir -p "${TEMP_DIR}/extracted"
tar -xzf "${TEMP_DIR}/upstream.tar.gz" -C "${TEMP_DIR}/extracted"

# 找到解壓後的頂層資料夾
EXTRACTED_ROOT="$(find "${TEMP_DIR}/extracted" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
SOURCE_DIR="${EXTRACTED_ROOT}/main/xiaozhi-server"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "❌ 錯誤：在下載的包裝中找不到 main/xiaozhi-server 目錄。"
    exit 1
fi

echo "==> 正在同步更新檔案到本地..."
# 排除 .git, .gitignore, sync-upstream.sh 與自訂外掛目錄 plugins_func 中的自寫工具
rsync -av --delete \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='sync-upstream.sh' \
    --exclude='README.md' \
    --exclude='data/' \
    --exclude='tmp/' \
    --exclude='plugins_func/functions/*' \
    "$SOURCE_DIR/" "$PROJECT_ROOT/"

echo ""
echo "✅ 官方 ${TARGET} 檔案同步完成！"
echo "==> 變更檔案清單："
git status -s
echo ""
echo "💡 後續建議操作："
echo "1. 執行 'git diff' 檢查官方變更（確認自訂的 gemini/edge-tts 修補是否需要保留）。"
echo "2. 執行 'docker restart xiaozhi-esp32-server' 驗證服務運行。"
echo "3. 確認無誤後，以單一 commit 提交："
echo "   git add ."
echo "   git commit -m \"chore: sync with upstream ${TARGET}\""
echo "   git push origin main"
