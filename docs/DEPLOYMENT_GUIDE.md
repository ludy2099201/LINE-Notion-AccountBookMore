# 雲端平台部署與端對端驗證手冊

本手冊用於指引您將 `LINE-Notion-AccountBook` 部署至 **Railway** 或 **Heroku** 雲端平台，並進行最終的端對端（End-to-End）驗證。

---

## 📦 部署前準備

請先確認專案內容皆已完整且包含以下檔案：
- `app/main.py`
- `requirements.txt`
- `Procfile`
- `.env.example`

您需要提前準備好寫入雲端平台的 **環境變數（Environment Variables）** 清單：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `GEMINI_API_KEY`
- `LLM_MODEL=models/gemini-2.5-flash`
- `VISION_MODEL=models/gemini-2.5-flash`
- `SCHOOL_NAME=慕熙大世界補習班`
- `APP_HOST=0.0.0.0`
- `APP_PORT=3000` (如果平台沒有自動注入 `PORT` 變數)
- `APP_DEBUG=false`

---

## 🛤 方案 A：Railway 部署步驟

### 1. 建立 Railway 專案
1. 登入 [Railway](https://railway.app/)。
2. 點擊 `New Project`。
3. 選擇 `Deploy from GitHub repo`，授權並選擇本專案 repository。

### 2. 設定啟動行為
Railway 通常能自動辨識 Python 專案與 `Procfile`，若無自動偵測，可設定 Start Command：
`gunicorn app.main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

### 3. 設定環境變數
1. 進入專案的 `Variables` 頁籤。
2. 將上述「部署前準備」列出的環境變數逐一加入（`APP_PORT` 在 Railway 通常由平台注入的 `PORT` 自動替代）。

### 4. 取得公開網址
1. 部署成功後，前往 Railway 的 `Settings` 頁面生成一個公開網域（Public Domain）。
2. 記下基底網址，例如：`https://your-app.up.railway.app`。
   - **健康檢查（Health Check）網址為**：`https://your-app.up.railway.app/`
   - **Webhook URL 為**：`https://your-app.up.railway.app/webhook`

---

## ☁️ 方案 B：Heroku 部署步驟

### 1. 建立 Heroku App
1. 登入 [Heroku Dashboard](https://dashboard.heroku.com/)。
2. 選擇 `New` → `Create new app`。
3. 設定 App 名稱與區域 (Region)。
4. 建立完成後，可於 Deploy 頁籤連接 GitHub repository 或使用 Heroku CLI 上傳程式碼。

### 2. 設定 Build / Runtime
- 確認已經掛載 Python Buildpack。
- 專案根目錄的 `Procfile` 內容應為：`web: gunicorn app.main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`。

### 3. 設定 Config Vars
1. 前往 `Settings` → 進入 `Reveal Config Vars`。
2. 將上述「部署前準備」列出的環境變數逐一加入。

### 4. 部署並取得網址
1. 在 Deploy 頁籤點選 `Deploy Branch`。
2. 成功後，記下：
   - **健康檢查（Health Check）網址為**：`https://your-app.herokuapp.com/`
   - **Webhook URL 為**：`https://your-app.herokuapp.com/webhook`

---

## 🩺 第一波驗證：健康檢查 (Health Check)

部署完成後，請先在瀏覽器打開根目錄網址 (`/`)，預期應回傳 JSON：
```json
{
  "llm_engine": "active",
  "ocr_engine": "active",
  "school": "慕熙大世界補習班",
  "service": "LINE-Notion-AccountBook",
  "status": "ok",
  "version": "3.1.0"
}
```

🚨 **若首頁無法開啟 (Application Error)**：
- 檢查部署日誌 (Logs)，是否因缺少環境變數，被 `validate_config()` 在啟動時擋下（Fail-fast）。
- 檢查 gunicorn 啟動命令是否正確執行。

---

## 🔗 第二波驗證：Webhook 串接

1. 確認健康檢查成功後，組裝 Webhook 網址（`https://.../webhook`）。
2. 回到 LINE Developers Console → Messaging API 分頁。
3. 將 Webhook URL 設定為您的網址並點擊 **Verify**。
4. 成功後開啟 **Use webhook** 開關（詳細 LINE 操作請參閱 [LINE_WEBHOOK_SETUP.md](LINE_WEBHOOK_SETUP.md)）。

---

## ✅ 第三波驗證：端對端 (End-to-End) 功能驗證

### 1. 文字記帳
- **傳送訊息**：`買了 30 本數學講義 $4500`
- **預期結果**：
  - LINE 收到記帳成功的回覆卡片。
  - Notion 資料庫新增一筆「教材費」且為「支出」$4,500 的資料。

### 2. OCR 圖片記帳
- **傳送訊息**：一張清晰完整的發票或收據照片。
- **預期結果**：
  - 系統瞬間回覆「📸 收到圖片，正在進行 OCR 辨識中...」。
  - 約 5-15 秒後收到由 push message 發送出的最終解析與紀錄結果。
  - Notion 資料庫新增該發票的品項與總額等 OCR 紀錄。

### 3. 指令驗證
- **傳送訊息**：`/help` 與 `/status`
- **預期結果**：系統應分別回傳幫助選單與系統目前的服務啟動狀態。

### 4. 錯誤場景驗證防呆
- **傳送無法辨識金額的文字**：系統應不崩潰，並回覆正確格式提示。
- **傳送風景照或模糊照片**：系統應提示非發票圖片或辨識失敗，要求重新拍攝或改用文字。

---

## 🚑 常見問題排查

### ❌ 應用啟動失敗 / 503 Error
- **環境變數缺漏**：請至 Logs 中尋找 `Configuration validation failed` 的錯誤訊息，補齊缺少的 Key。
- **Procfile**：指令設定有誤。

### ❌ Webhook Verify 失敗
- **未對外公開**：網域不正確。
- **網址沒加 `/webhook`**：網址只貼了根目錄。
- **服務正在休眠**：(例如 Heroku 的免費層/基本層休眠機制)，再點一次 `Verify` 嘗試喚醒。

### ❌ Notion 無法寫入
- **API Key 或 Database ID 錯誤**。
- Notion Integration 邀請尚未分享（Add connections）至您的記帳資料庫。

### ❌ OCR 無法運作，或逾時無回應
- **圖片過大**：超過 10MB 將被系統擋下。
- **GEMINI_API_KEY 無效**：確認您的 Google AI Studio 額度未達上限。
- **Webhook 逾時**：確認背景執行緒 (Threading) 正常啟動，且系統可以透過 `push_message` 發送延遲訊息給您（請確認是否允許 LINE 機器人 Push Message 給您）。
