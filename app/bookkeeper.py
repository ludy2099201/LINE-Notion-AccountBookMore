"""記帳流程：解析 → 檢核 → 寫入 Notion → 組出回覆文字。

與 LINE / Flask 無關，方便單獨測試。main.py 只負責收事件與送訊息。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app import ledger_rules
from app.data_processor import AccountEntry, DataProcessor
from app.notion_client import UNDO_WINDOW_HOURS, NotionAccountClient, NotionClientError

logger = logging.getLogger(__name__)

# LINE 單則文字訊息上限 5000 字
MAX_REPLY_LENGTH = 4800
UNKNOWN_USER = "Unknown"

CORRECTION_REPLY = (
    "✋ 這則看起來是要修改先前的紀錄，所以沒有新增任何資料。\n\n"
    "目前還不能直接用訊息修改舊資料，請擇一處理：\n"
    "1. 回覆「刪除上一筆」撤銷您最近記的那一筆，再把正確內容重新傳一次\n"
    "2. 直接到 Notion 修改該筆紀錄"
)

PARSE_FAILURE_REPLY = (
    "無法解析金額，請重新輸入。\n\n"
    "支援格式：\n"
    "1. 自由輸入：買了 30 本講義 $4500\n"
    "2. 結構化：摘要: 講義 | 金額: 4500 | 類型: 支出\n"
    "3. 指令：/支 講義 4500\n"
    "4. 📸 直接拍發票照片"
)

UNDO_REDELIVERY_REPLY = (
    "這則「刪除上一筆」是 LINE 重新送達的，為了避免多刪一筆，這次沒有執行。\n"
    "請到 Notion 確認；如果剛才那筆還在，請再傳一次「刪除上一筆」。"
)


class Bookkeeper:
    """把一則使用者輸入變成 Notion 上的記帳紀錄，並回傳要回覆的文字。"""

    def __init__(self, processor: DataProcessor, notion: NotionAccountClient) -> None:
        self.processor = processor
        self.notion = notion

    # ── 文字訊息 ─────────────────────────────────────────

    def handle_text(
        self,
        text: str,
        user_name: str,
        is_redelivery: bool = False,
        event_time: Optional[datetime] = None,
    ) -> str:
        """處理一則文字訊息，回傳要回覆給使用者的文字。

        Args:
            is_redelivery: LINE 標示這則事件是重新送達。
            event_time: 使用者送出訊息的時間（LINE 事件的 timestamp）。
                重送時用來分辨「這則訊息第一次送達時寫入的紀錄」和
                「更早以前另一筆內容相同的紀錄」。
        """
        if ledger_rules.is_undo_command(text):
            if is_redelivery:
                # 重送的「刪除上一筆」若再執行一次，會連前一筆也刪掉
                return UNDO_REDELIVERY_REPLY
            return self.undo_last(user_name)

        if ledger_rules.is_correction_message(text):
            return CORRECTION_REPLY

        dry_run = ledger_rules.is_dry_run_message(text)
        if dry_run:
            text = ledger_rules.strip_dry_run_prefix(text)
            if not text:
                return (
                    "🧪 測試模式：在「測試」後面接記帳內容，我會顯示解析結果但不寫入 Notion。\n"
                    "例如：測試 買講義 500"
                )

        message = self.processor.process_text_multi(text)
        recordable = [entry for entry in message.entries if entry.is_recordable]
        skipped = [entry for entry in message.entries if entry.title and not entry.is_recordable]
        if not recordable:
            return PARSE_FAILURE_REPLY

        warnings = list(message.warnings)
        if skipped:
            names = "、".join(entry.title for entry in skipped)
            warnings.append(f"以下項目沒有金額，沒有記錄：{names}")

        if dry_run:
            return self._trim(
                self.processor.format_batch_confirmation(recordable, warnings, written=False)
            )

        return self._record(recordable, warnings, user_name, is_redelivery, event_time)

    # ── 圖片（OCR 結果）─────────────────────────────────

    def handle_ocr_entry(self, entry: AccountEntry, user_name: str) -> str:
        """把 OCR 辨識出的一筆寫入 Notion，回傳要推送給使用者的文字。"""
        return self._record([entry], [], user_name, is_redelivery=False)

    # ── 撤銷 ─────────────────────────────────────────────

    def undo_last(self, user_name: str) -> str:
        """撤銷該使用者最近記的一筆（移到 Notion 垃圾桶）。"""
        if not user_name or user_name == UNKNOWN_USER:
            return "無法確認您的身分（讀不到 LINE 名稱），所以沒有刪除任何資料。請直接到 Notion 處理。"
        try:
            last = self.notion.find_last_entry_by(user_name)
            if not last:
                return (
                    f"找不到您在 {UNDO_WINDOW_HOURS} 小時內記錄的資料，沒有刪除任何東西。\n"
                    "更早的紀錄請直接到 Notion 修改。"
                )
            self.notion.archive_entry(last["id"])
        except NotionClientError as exc:
            return f"撤銷失敗：{exc}\n沒有刪除任何資料，請直接到 Notion 處理。"

        amount = last.get("amount")
        amount_text = f" ${amount:,.0f}" if isinstance(amount, (int, float)) else ""
        return (
            f"🗑 已刪除您最近記的一筆：{last['title']}{amount_text}\n"
            "（已移到 Notion 垃圾桶，30 天內可還原。要再刪前一筆請再傳一次「刪除上一筆」）"
        )

    # ── 內部 ─────────────────────────────────────────────

    def _record(
        self,
        entries: list,
        warnings: list,
        user_name: str,
        is_redelivery: bool,
        event_time: Optional[datetime] = None,
    ) -> str:
        written: list = []
        failures: list = []
        already_recorded: list = []

        for entry in entries:
            matches = self._similar_existing(entry)
            if is_redelivery and any(
                ledger_rules.title_key(m["title"]) == ledger_rules.title_key(entry.title)
                and m.get("recorder") == user_name
                and self._created_since(m, event_time)
                for m in matches
            ):
                # LINE 重送同一則訊息，而且第一次其實已經寫進去了
                already_recorded.append(entry)
                continue
            if matches:
                entry.warnings.append(
                    f"帳本裡 {entry.date} 已經有一筆「{matches[0]['title']}」"
                    f"${entry.amount:,.0f}，這筆可能重複。"
                    "若是重複，請回覆「刪除上一筆」"
                )
            try:
                self.notion.create_account_entry(
                    title=entry.title,
                    amount=entry.amount,
                    entry_type=entry.entry_type,
                    category=entry.category,
                    date=entry.date,
                    vendor=entry.vendor,
                    payment_method=entry.payment_method,
                    description=entry.description,
                    invoice_number=entry.invoice_number,
                    tax_amount=entry.tax_amount,
                    source_user=user_name,
                    period=entry.period,
                )
                written.append(entry)
            except NotionClientError as exc:
                logger.error("Failed to write entry %r: %s", entry.title, exc)
                failures.append((entry, str(exc)))

        if already_recorded and not written and not failures:
            logger.info("Skipped redelivered message: all entries already recorded")
            return "這則訊息先前已經記錄過了，沒有重複新增。"

        if not written:
            first_error = failures[0][1] if failures else "未知錯誤"
            return f"寫入 Notion 時發生錯誤：{first_error}\n請確認資料庫設定。沒有任何資料被寫入。"

        if failures:
            names = "、".join(f"{entry.title} ${entry.amount:,.0f}" for entry, _ in failures)
            warnings.append(f"以下 {len(failures)} 筆寫入失敗，請重新傳一次：{names}（{failures[0][1][:200]}）")

        return self._trim(self.processor.format_batch_confirmation(written, warnings))

    def _similar_existing(self, entry: AccountEntry) -> list:
        """查帳本裡同日、同金額、同類型且標題相近的紀錄；查不到就當作沒有。"""
        if not entry.date:
            return []
        try:
            candidates = self.notion.find_possible_duplicates(
                entry.date, entry.amount, entry.entry_type
            )
        except NotionClientError as exc:
            # 重複檢查只是提醒，查詢失敗不能擋住記帳
            logger.warning("Duplicate check skipped: %s", exc)
            return []
        return [c for c in candidates if ledger_rules.titles_similar(c["title"], entry.title)]

    @staticmethod
    def _created_since(match: dict, event_time: Optional[datetime]) -> bool:
        """這筆既有紀錄是不是在使用者送出訊息之後才建立的。

        在訊息送出之前就存在的紀錄不可能是這則訊息寫的，那是另一筆
        （例如同一天買了兩次同樣的東西），不能因此略過這次的寫入。
        時間無法判斷時視為是，寧可少寫也不重複。
        """
        if event_time is None:
            return True
        try:
            created = datetime.fromisoformat(str(match.get("created_time", "")).replace("Z", "+00:00"))
        except ValueError:
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        # Notion 的建立時間只精確到分鐘
        return created >= event_time.replace(second=0, microsecond=0)

    @staticmethod
    def _trim(text: str) -> str:
        if len(text) <= MAX_REPLY_LENGTH:
            return text
        return text[:MAX_REPLY_LENGTH] + "\n…（訊息過長，其餘請到 Notion 查看）"
