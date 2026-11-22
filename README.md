# LINE-Notion-AccountBook 補習班記帳系統 (v3.1.0)

專為**補習班日常收支管理**設計的自動化記帳系統。老師或行政人員只需在 LINE 中輸入記帳文字，或是直接拍照上傳發票/收據，系統便會透過 **Gemini Vision OCR** 與 **LLM 智慧解析引擎** 將資料解析為結構化內容，並自動寫入 Notion 的記帳資料庫。

## 🌟 核心特色 (v3.1.0 新增與優化)

- **Gemini Vision OCR 圖片解析**：上傳發票或收據，自動辨識金額、消費對象與品項。
- **Gemini LLM 智慧分類**：內建 14 種補習班專用分類（如：教材費、人事費用、學費收入），由 LLM 自動從自然語言中萃取並分類。
- **雙引擎降級機制 (Dual-Engine Fallback)**：當 LLM 服務失敗時，無縫降級至 Rule Engine（關鍵字與正則表達式解析），確保服務不中斷。
- **非同步事件處理**：圖片 OCR 處理耗時較長，系統採用背景 Threading 處理，避免 LINE Webhook 發生 504 Timeout。
- **穩定的 API 重試機制**：對外連線（呼叫 Gemini API）全面實作指數退避重試（Exponential Backoff）。
- **Fail-Fast 組態驗證**：應用啟動時立即透過 `validate_config()` 確認環境變數是否齊全，提早報錯。
- **高覆蓋率測試**：內建 180 多項單元與整合測試（基於 pytest），涵蓋各種輸入格式、錯誤處理與邊界情境。

---

## 🏗 系統架構

1. **Input Layer (LINE)**: 接收使用者的文字記帳訊息或圖片（圖片限制 10MB，超出自動退回）。
2. **Processing Layer (Flask + Gemini)**: 
   - **文字分支**：解析指令或丟入 LLM 解析收支、金額與分類。如果失敗，則交給 Rule Engine 收尾處理。
   - **圖片分支**：下載圖片交給 Gemini Vision 進行結構化 OCR 解析。
3. **Output Layer (Notion)**: 透過 Notion API 將 12 個欄位的完整資料（包含稅額、發票號碼等）寫入指定資料庫，並透過 LINE 回傳確認訊息。

---

## 🛠 專案結構

```
LINE-Notion-AccountBook/
├── app/
│   ├── __init__.py
│   ├── main.py              # Flask 應用程式與 Webhook 進入點
│   ├── line_handler.py      # LINE Messaging API（簽章驗證、圖片下載、Push/Reply）
│   ├── ocr_engine.py        # Gemini Vision API 圖片辨識引擎
│   ├── llm_parser.py        # Gemini API 自然語言記帳解析引擎
│   ├── data_processor.py    # 雙引擎資料處理器（LLM/OCR/Rule Fallback）
│   ├── ledger_rules.py      # 資料品質規則（分類、日期/歸屬月份、重複、金額檢核）
│   ├── bookkeeper.py        # 記帳流程（查重 → 寫入 → 回覆、撤銷、測試模式）
│   └── notion_client.py     # Notion API 資料庫寫入層（12欄位）
├── config/
│   ├── __init__.py
│   └── settings.py          # 環境變數設定與驗證模組 (Fail-fast)
├── tests/
│   ├── test_processor.py    # DateProcessor 雙引擎整合測試
│   ├── test_ledger_rules.py # 資料品質規則測試
│   ├── test_bookkeeper.py   # 記帳流程測試
│   ├── test_notion_client.py# Notion 寫入 / 查詢測試
│   ├── test_llm_parser.py   # LLM 引擎單元測試
│   └── test_ocr_engine.py   # OCR 引擎單元測試
├── docs/                    # 各種文件與設定手冊
├── requirements.txt         # 依賴套件 (Flask, requests, python-dotenv, 等)
└── .env.example             # 環境變數範例檔
```

---

## 🚀 部署與執行

### 一、事前準備
1. 前往 **LINE Developers** 建立 Messaging API Channel，取得 `Channel Secret` 與 `Access Token`。（詳細流程請見：[LINE Webhook 設定手冊](docs/LINE_WEBHOOK_SETUP.md)）
2. 前往 **Notion** 建立一個資料庫（需包含指定的 12 個欄位），並取得 Internal Integration Token 與 Database ID。
3. 前往 **Google AI Studio** 取得 `GEMINI_API_KEY`（必須支援 Vision 模型）。

### 二、環境變數 (`.env`)
複製 `.env.example` 為 `.env`，並填寫以下設定：

```env
# APP Config
APP_HOST=0.0.0.0
APP_PORT=3000
APP_DEBUG=False
SCHOOL_NAME=我的補習班

# LINE Config
LINE_CHANNEL_ACCESS_TOKEN=your_line_token
LINE_CHANNEL_SECRET=your_line_secret

# Notion Config
NOTION_API_KEY=secret_your_notion_key
NOTION_DATABASE_ID=your_notion_database_id

# LLM / OCR Engine Config
GEMINI_API_KEY=your_gemini_api_key
```

### 三、本機啟動
```bash
# 1. 建立虛擬環境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 測試（需全數通過）
pytest tests/ -v

# 4. 啟動伺服器
python -m app.main
```

---

## 💬 支援的使用指令與格式

在 LINE 中向您的機器人發送以下內容：

1. **文字輸入**（由 LLM 智慧解析）：
   - `買了 30 本數學講義 $4500`
   - `收到王小明下個學期的學費 15000`
2. **圖片輸入**（發票 / 收據）：
   - 直接將發票或收據拍照上傳。系統會顯示「OCR 辨識中...」，解析完成後會 Push 記帳結果卡片。
3. **結構化輸入**：
   - `摘要: 影印費 | 金額: 300 | 類型: 支出 | 分類: 辦公用品`
4. **快速指令**：
   - `/支 影印紙 500`
   - `/收 張三學費 8000`
5. **一則訊息多筆**：同一則訊息列出多位學生／多張帳單時，會自動拆成多筆寫入；訊息若寫了「總共 $121,500」，會核對各筆加總是否相符。
6. **日期與歸屬月份**：
   - `10/4 付 8月勞保 11122 轉帳` → 日期 `10/4`（實際付款日）、歸屬月份 `8 月`。
   - 訊息沒寫日期時以今天（台灣時間）計，回覆會註明「以今天計」。
7. **撤銷**：`刪除上一筆`（或 `/undo`）會把您 24 小時內最近記的一筆移到 Notion 垃圾桶。
8. **測試不寫入**：`測試 買講義 500` 只顯示解析結果，不寫入 Notion。
9. **系統輔助指令**：
   - `/help` 或 `幫助`：顯示系統操作說明。
   - `/status` 或 `狀態`：查看系統各模組健康狀態（含 LLM 與 OCR 啟用狀態）。

---

## 📄 Notion 資料庫欄位結構 (Schema)

請在 Notion 中建立以下欄位：
- **摘要** (Title)
- **金額** (Number)
- **收支類型** (Select: `收入`, `支出`)
- **分類** (Select: 14 種預設分類，如 `教材費`, `學費收入`... 等)
- **日期** (Date)
- **對象/廠商** (Rich text)
- **付款方式** (Select)
- **備註** (Rich text)
- **發票號碼** (Rich text)
- **稅額** (Number)
- **記錄人** (Rich text)
- **記錄時間** (Date)
- **歸屬月份** (Select、Text 或 Date，選填)：建立後 bot 會自動填入 `YYYY-MM`；沒有這個欄位時，歸屬月份與日期不同月的紀錄會改寫在備註開頭（`〔歸屬月份 2026-08〕`）。欄位名稱可用環境變數 `NOTION_PERIOD_PROPERTY` 調整。

> Notion integration 除了「插入內容」，還需要「讀取內容」與「更新內容」權限，重複提醒與「刪除上一筆」才能運作；沒有讀取權限時仍可正常記帳，只是不會提醒重複。

---

## 🛡 寫入前的檢核

| 檢核 | 行為 |
|---|---|
| 分類固定規則 | 管理費／房租／水電 → 房租水電；薪資／獎金／勞退 → 人事費用；勞保／健保／稅 → 保險稅務；課程／Workshop → 學費收入 |
| 分類、付款方式 | 一律收斂到固定清單，不會在 Notion 產生「未知」這類新選項 |
| 可能重複 | 同日、同金額、同類型且摘要相近時照常寫入，但在回覆中提醒並告知可「刪除上一筆」 |
| LINE 重送 | 同一則訊息被 LINE 重送且已寫入過時不會再寫一次 |
| 明細加總 | 薪資等有加減明細的訊息，加總與實付金額不符時提醒；內含代墊款時提醒勿重複記帳 |
| 修正訊息 | 「請修正…」開頭的訊息不會新增紀錄，會回覆處理方式 |
| 沒有金額 | 不寫入（含 OCR 沒辨識到金額的發票） |
