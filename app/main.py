"""LINE-Notion-AccountBook 主入口。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request

from app.bookkeeper import Bookkeeper
from app.data_processor import DataProcessor
from app.line_handler import LineSignatureError, LineWebhookHandler
from app.notion_client import NotionAccountClient
from config.settings import ConfigValidationError, app_config, validate_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 啟動環境驗證（優化 ⑤）────────────────────────────
try:
    validate_config()
except ConfigValidationError as exc:
    logger.error("Configuration validation failed: %s", exc)
    raise

# ── 服務初始化 ────────────────────────────────────────
app = Flask(__name__)
line_handler = LineWebhookHandler()
notion_client = NotionAccountClient()
data_processor = DataProcessor()
bookkeeper = Bookkeeper(data_processor, notion_client)


# ── 路由 ──────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health_check():
    """健康檢查端點，回傳 LLM / OCR 引擎狀態。"""
    return jsonify({
        "status": "ok",
        "service": "LINE-Notion-AccountBook",
        "version": "3.1.0",
        "school": app_config.school_name,
        "llm_engine": "active" if data_processor.llm_parser.is_available() else "inactive",
        "ocr_engine": "active" if data_processor.ocr_engine.is_available() else "inactive",
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """LINE Webhook 進入點。驗證簽章後分派事件。"""
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400, description="Missing X-Line-Signature header")

    body = request.get_data(as_text=True)
    try:
        line_handler.verify_signature(body, signature)
    except LineSignatureError:
        abort(403, description="Invalid signature")

    events = line_handler.parse_events(body)
    for event in events:
        try:
            _process_event(event)
        except Exception as exc:
            logger.error("Error processing event: %s", exc, exc_info=True)

    return "OK", 200


# ── 事件處理 ──────────────────────────────────────────

def _process_event(event: dict) -> None:
    """處理單一 LINE 事件（文字 / 圖片 / 指令）。"""
    user_id = line_handler.get_user_id(event)
    reply_token = line_handler.get_reply_token(event)

    user_name = "Unknown"
    if user_id:
        profile = line_handler.get_user_profile(user_id)
        if profile:
            user_name = profile.get("displayName", "Unknown")

    # ── 文字訊息 ──────────────────────────
    text = line_handler.extract_text_message(event)
    if text:
        if text.lower() in {"/help", "help", "幫助"}:
            _send_help(reply_token)
            return
        if text.lower() in {"/status", "status", "狀態"}:
            _send_status(reply_token)
            return

        # LINE 在 webhook 逾時後會重送同一則事件；交給 Bookkeeper 判斷是否已記過
        is_redelivery = bool(event.get("deliveryContext", {}).get("isRedelivery"))
        event_time = _event_time(event)
        # 一則訊息可能拆成多筆、每筆都要查重再寫入，放到背景執行，
        # 讓 webhook 立刻回 200，LINE 才不會因逾時重送而造成重複記帳。
        # 不能用 daemon 執行緒：已經回了 200，LINE 不會再送一次，
        # worker 重啟（部署、回收）時要等這則寫完才能結束，否則這筆帳就不見了。
        threading.Thread(
            target=_process_text_async,
            args=(text, user_name, user_id, reply_token, is_redelivery, event_time),
            daemon=False,
        ).start()
        return

    # ── 圖片訊息（OCR 非同步）────────────
    image_id = line_handler.extract_image_message(event)
    if image_id:
        # reply_token 只能用一次 → 先回「辨識中」
        line_handler.reply_message(reply_token, "📸 收到圖片，正在進行 OCR 辨識中...")
        # Bug #4：背景執行緒處理 OCR，避免 webhook 504 timeout
        threading.Thread(
            target=_process_image_async,
            args=(image_id, user_name, user_id),
            daemon=True,
        ).start()


def _event_time(event: dict) -> datetime | None:
    """LINE 事件的 timestamp（毫秒）→ UTC 時間；沒有或格式不對回傳 None。"""
    try:
        return datetime.fromtimestamp(int(event["timestamp"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _process_text_async(
    text: str,
    user_name: str,
    user_id: str | None,
    reply_token: str | None,
    is_redelivery: bool,
    event_time: datetime | None = None,
) -> None:
    """背景執行緒：解析 → 檢核 → 寫 Notion → 回覆。"""
    try:
        reply = bookkeeper.handle_text(
            text, user_name, is_redelivery=is_redelivery, event_time=event_time
        )
    except Exception as exc:  # 背景執行緒的例外不會回到 webhook，這裡要自己回報
        logger.error("Error handling text message: %s", exc, exc_info=True)
        reply = "處理這則訊息時發生錯誤，沒有確認是否寫入。請到 Notion 檢查後再重傳。"

    # reply token 約一分鐘失效；處理太久時改用 push 才不會讓使用者收不到結果
    if reply_token and line_handler.reply_message(reply_token, reply):
        return
    if user_id:
        line_handler.push_message(user_id, reply)


def _process_image_async(image_id: str, user_name: str, user_id: str | None) -> None:
    """背景執行緒：下載圖片 → OCR → 寫 Notion → push 確認（優化 ⑧）。"""
    if not user_id:
        logger.warning("Skip OCR push flow: missing user_id")
        return

    image_bytes = line_handler.download_image(image_id)
    if not image_bytes:
        line_handler.push_message(user_id, "圖片下載失敗或超過 10MB，請重新上傳。")
        return

    entry = data_processor.process_image(image_bytes)
    if not entry.is_recordable:
        # 沒有金額的發票不寫入，避免帳本出現 $0 的紀錄
        logger.warning("OCR failed or no amount detected for image: %s", image_id)
        line_handler.push_message(user_id, "OCR 沒有辨識到金額，沒有記錄。請改用文字輸入或重新拍攝清晰照片。")
        return

    line_handler.push_message(user_id, bookkeeper.handle_ocr_entry(entry, user_name))


# ── 指令回應 ───────────────────────────────────────────

def _send_help(reply_token: str) -> None:
    help_text = (
        f"📖 {app_config.school_name} 記帳系統\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 文字記帳：\n  買了 30 本數學講義 $4,500\n\n"
        "📸 拍照記帳：\n  直接拍發票或收據照片\n\n"
        "📋 結構化輸入：\n  摘要: 講義 | 金額: 4500 | 類型: 支出\n\n"
        "⌨️ 指令格式：\n  /支 數學講義 4500\n  /收 王小明學費 12000\n\n"
        "🧾 一次多筆：\n  同一則訊息列出多筆，會自動拆開記錄\n\n"
        "📅 日期與月份：\n  10/4 付 8月勞保 11122 轉帳\n"
        "  （10/4 是付款日，8 月是歸屬月份；沒寫日期以今天計）\n\n"
        "↩️ 記錯了：\n  刪除上一筆 - 撤銷您最近記的一筆\n\n"
        "🧪 試試看不寫入：\n  測試 買講義 500\n\n"
        "🔧 其他指令：\n  /help - 顯示此說明\n  /status - 查看系統狀態"
    )
    line_handler.reply_message(reply_token, help_text)


def _send_status(reply_token: str) -> None:
    llm = "已啟用" if data_processor.llm_parser.is_available() else "未設定"
    ocr = "已啟用" if data_processor.ocr_engine.is_available() else "未設定"
    period = (
        "已啟用" if notion_client.has_period_property()
        else f"未建立（在 Notion 新增「{notion_client.period_property}」欄位即可啟用）"
    )
    status_text = (
        f"✅ {app_config.school_name} 記帳系統\n\n"
        f"🧠 LLM 智慧解析：{llm}\n"
        f"📸 OCR 發票辨識：{ocr}\n"
        f"🔗 Notion 連線：正常\n"
        f"🗓 歸屬月份欄位：{period}"
    )
    line_handler.reply_message(reply_token, status_text)


if __name__ == "__main__":
    app.run(host=app_config.host, port=app_config.port, debug=app_config.debug)
