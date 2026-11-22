"""記帳資料處理器 — 雙引擎架構。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app import ledger_rules
from app.llm_parser import (
    EXPENSE_CATEGORIES,
    LLMParser,
    normalize_date,
    normalize_payment_method,
    normalize_period,
)
from app.ocr_engine import OCREngine
from config.settings import local_now

logger = logging.getLogger(__name__)

# Bug #3 修正：移除模糊單字，改用完整詞組
INCOME_PHRASES = ["收到", "繳費", "入帳", "退款", "學費"]
EXPENSE_PHRASES = ["買", "付", "支付", "花", "購", "修", "租"]

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "教材費":   ["講義", "教科書", "課本", "文具", "教材", "書"],
    "房租水電": ["房租", "租金", "管理費", "水費", "電費", "網路", "瓦斯"],
    "人事費用": ["薪資", "薪水", "工讀", "助教", "鐘點費", "獎金", "勞退"],
    "保險稅務": ["勞保", "健保", "災保", "保險", "稅"],
    "設備維護": ["電腦", "投影機", "冷氣", "維修", "設備"],
    "辦公用品": ["影印", "墨水", "紙", "文具", "碳粉"],
    "餐飲點心": ["點心", "飲料", "便當", "餐", "零食"],
    "交通費":   ["交通", "停車", "油費", "計程車", "高鐵"],
    "廣告行銷": ["傳單", "廣告", "招牌", "海報"],
    "學費收入": ["學費", "補習費", "課程", "上課費", "Workshop", "workshop"],
    "報名費":   ["報名"],
    "教材收入": ["賣講義", "賣書"],
}


@dataclass
class AccountEntry:
    """結構化記帳資料。"""

    title: str = ""
    amount: float = 0.0
    entry_type: str = "支出"          # 收入 / 支出
    category: str = "其他支出"
    date: Optional[str] = None        # 實際收付日 YYYY-MM-DD
    period: Optional[str] = None      # 歸屬月份 YYYY-MM
    date_defaulted: bool = False      # True = 訊息沒提日期，以今天計
    vendor: str = ""
    payment_method: str = "未指定"
    description: str = ""
    invoice_number: str = ""
    tax_amount: float = 0.0
    raw_text: str = ""
    parse_engine: str = "rule"        # "llm" | "ocr" | "rule"
    parse_confidence: float = 0.0
    items: list = field(default_factory=list)
    components: list = field(default_factory=list)   # 金額的加減明細（薪資等）
    warnings: list = field(default_factory=list)     # 需要使用者確認的事項

    @property
    def is_recordable(self) -> bool:
        """有摘要且金額大於 0 才能寫入帳本。"""
        return bool(self.title) and self.amount > 0


@dataclass
class ParsedMessage:
    """一則訊息的解析結果（可能包含多筆）。"""

    entries: list = field(default_factory=list)
    warnings: list = field(default_factory=list)     # 整則訊息層級的提醒


class DataProcessor:
    """雙引擎記帳資料處理器（LLM + Rule Engine）。"""

    def __init__(self) -> None:
        self.llm_parser = LLMParser()
        self.ocr_engine = OCREngine()

    # ── 公開介面 ─────────────────────────────────────────

    def process_text(self, text: str) -> AccountEntry:
        """處理文字輸入的記帳訊息，回傳單筆（多筆時取第一筆）。

        需要一則訊息拆多筆時請用 process_text_multi()。
        """
        entries = self.process_text_multi(text).entries
        return entries[0] if entries else AccountEntry(raw_text=text.strip())

    def process_text_multi(self, text: str) -> ParsedMessage:
        """處理文字輸入的記帳訊息（自由 / 結構化 / 指令格式），可回傳多筆。"""
        text = text.strip()
        if not text:
            return ParsedMessage(entries=[AccountEntry(raw_text=text)])

        if "|" in text and ":" in text:
            message = ParsedMessage(entries=[self._parse_structured(text)])
        elif text.startswith(("/record", "/收", "/支")):
            message = ParsedMessage(entries=[self._parse_command(text)])
        else:
            message = self._parse_with_llm_fallback(text)

        for entry in message.entries:
            entry.raw_text = text
            self._finalize(entry)

        if len(message.entries) > 1:
            self._flag_duplicates_within_message(message.entries)
        return message

    def process_image(self, image_bytes: bytes) -> AccountEntry:
        """處理圖片（發票/收據），透過 OCR 辨識後轉為 AccountEntry。"""
        if not self.ocr_engine.is_available():
            return AccountEntry(
                title="OCR 辨識失敗",
                description="OCR 引擎未啟用",
                parse_engine="ocr",
                parse_confidence=0.0,
            )

        ocr_result = self.ocr_engine.recognize(image_bytes)
        if not ocr_result:
            return AccountEntry(
                title="OCR 辨識失敗",
                description="無法辨識圖片內容",
                parse_engine="ocr",
                parse_confidence=0.0,
            )

        if "error" in ocr_result:
            return AccountEntry(
                title=ocr_result.get("error", "非發票圖片"),
                description=ocr_result.get("raw_text", ""),
                parse_engine="ocr",
                parse_confidence=0.1,
            )

        entry = AccountEntry(
            title=self._build_ocr_title(ocr_result),
            amount=abs(self._safe_float(ocr_result.get("total_amount"))),
            entry_type="支出",
            category=self._classify_ocr(ocr_result),
            date=ocr_result.get("date"),
            vendor=ocr_result.get("vendor") or "",
            payment_method=ocr_result.get("payment_method") or "未指定",
            description=ocr_result.get("raw_text") or "",
            invoice_number=ocr_result.get("invoice_number") or "",
            tax_amount=abs(self._safe_float(ocr_result.get("tax_amount"))),
            items=ocr_result.get("items") or [],
            parse_engine="ocr",
            parse_confidence=self._safe_float(ocr_result.get("confidence"), 0.8),
        )
        # 發票上的日期就是交易日；歸屬月份跟著交易日，不從發票全文猜
        self._finalize(entry, detect_period_from_text=False)
        return entry

    def format_confirmation(self, entry: AccountEntry, written: bool = True) -> str:
        """產出單筆的 LINE 確認訊息。written=False 為預覽（未寫入）。"""
        icon = "💰" if entry.entry_type == "收入" else "💸"
        engine_label = {
            "llm":  "AI 智慧解析",
            "ocr":  "發票 OCR 辨識",
            "rule": "規則引擎解析",
        }.get(entry.parse_engine, "解析")

        header = (
            f"{icon} 已記錄到 Notion 資料庫！" if written
            else "🧪 測試模式：以下內容「沒有」寫入 Notion"
        )
        lines = [
            header,
            f"（{engine_label}，信心度 {entry.parse_confidence:.0%}）",
            "",
            f"📋 摘要：{entry.title}",
            f"{icon} 類型：{entry.entry_type}",
            f"💵 金額：${entry.amount:,.0f}",
            f"📁 分類：{entry.category}",
        ]
        if entry.date:
            suffix = "（訊息沒寫日期，以今天計）" if entry.date_defaulted else ""
            lines.append(f"📅 日期：{entry.date}{suffix}")
        if entry.period and entry.period != (entry.date or "")[:7]:
            lines.append(f"🗓 歸屬月份：{entry.period}")
        if entry.vendor:
            lines.append(f"🏪 對象：{entry.vendor}")
        if entry.invoice_number:
            lines.append(f"🧾 發票：{entry.invoice_number}")
        if entry.payment_method != "未指定":
            lines.append(f"💳 付款：{entry.payment_method}")
        if entry.items:
            lines.append(f"📦 品項：共 {len(entry.items)} 項")

        for warning in entry.warnings:
            lines.append(f"⚠️ {warning}")

        missing = self.missing_fields(entry)
        if missing:
            lines.append(f"💡 沒填：{'、'.join(missing)}（下次可在訊息裡一起寫）")
        return "\n".join(lines)

    def format_batch_confirmation(
        self,
        entries: list,
        message_warnings: Optional[list] = None,
        written: bool = True,
    ) -> str:
        """產出多筆的 LINE 確認訊息（每筆一行摘要 + 需要確認的事項）。"""
        if len(entries) == 1 and not message_warnings:
            return self.format_confirmation(entries[0], written=written)

        income = sum(e.amount for e in entries if e.entry_type == "收入")
        expense = sum(e.amount for e in entries if e.entry_type == "支出")
        header = (
            f"✅ 已記錄 {len(entries)} 筆到 Notion 資料庫！" if written
            else f"🧪 測試模式：以下 {len(entries)} 筆「沒有」寫入 Notion"
        )
        lines = [header, ""]
        for index, entry in enumerate(entries, start=1):
            icon = "💰" if entry.entry_type == "收入" else "💸"
            detail = f"{index}. {icon} {entry.title} ${entry.amount:,.0f}｜{entry.category}"
            if entry.period and entry.period != (entry.date or "")[:7]:
                detail += f"｜歸屬 {entry.period}"
            lines.append(detail)
            for warning in entry.warnings:
                lines.append(f"   ⚠️ {warning}")

        lines.append("")
        if income:
            lines.append(f"收入合計：${income:,.0f}")
        if expense:
            lines.append(f"支出合計：${expense:,.0f}")

        dates = sorted({e.date for e in entries if e.date})
        if dates:
            defaulted = all(e.date_defaulted for e in entries)
            suffix = "（訊息沒寫日期，以今天計）" if defaulted else ""
            lines.append(f"📅 日期：{'、'.join(dates)}{suffix}")

        for warning in message_warnings or []:
            lines.append(f"⚠️ {warning}")

        missing = sorted({name for e in entries for name in self.missing_fields(e)})
        if missing:
            lines.append(f"💡 部分項目沒填：{'、'.join(missing)}")
        return "\n".join(lines)

    @staticmethod
    def missing_fields(entry: AccountEntry) -> list:
        """回傳這筆帳沒填、但對帳時會用到的欄位名稱。"""
        missing = []
        if entry.payment_method == "未指定":
            missing.append("付款方式")
        if not entry.vendor:
            missing.append("對象")
        return missing

    # ── 共用後處理 ───────────────────────────────────────

    def _finalize(self, entry: AccountEntry, detect_period_from_text: bool = True) -> None:
        """所有解析路徑（LLM / 規則 / 結構化 / OCR）共用的正規化與檢核。

        Notion 的 select 欄位遇到新值會自動新增選項，所以分類與付款方式
        一律在這裡收斂到固定清單，不依賴各解析引擎自己把關。
        """
        if not entry.title and entry.amount == 0:
            return

        today = local_now().date()

        entry.title = re.sub(r"\s+", " ", entry.title).strip()
        if entry.entry_type not in {"收入", "支出"}:
            entry.entry_type = "支出"
        entry.payment_method = normalize_payment_method(entry.payment_method)
        entry.vendor = ledger_rules.clean_optional(entry.vendor)
        entry.invoice_number = ledger_rules.clean_optional(entry.invoice_number)

        entry.category, category_warning = ledger_rules.canonical_category(
            entry.title, entry.entry_type, entry.category
        )
        if category_warning:
            entry.warnings.append(category_warning)

        # 備註只是重複摘要時不寫，避免帳本裡兩欄內容一樣
        if ledger_rules.title_key(entry.description) == ledger_rules.title_key(entry.title):
            entry.description = ""

        # 日期：實際收付日。訊息沒提就以今天計，並在回覆中明說
        entry.date = normalize_date(entry.date)
        if not entry.date:
            entry.date = today.strftime("%Y-%m-%d")
            entry.date_defaulted = True

        # 歸屬月份：這筆帳屬於哪個月（「8月勞保」在 10 月補記，歸屬仍是 8 月）
        entry.period = normalize_period(entry.period)
        if not entry.period and detect_period_from_text:
            entry.period = ledger_rules.detect_period(entry.title, today, entry.entry_type)
        if not entry.period:
            entry.period = entry.date[:7]

        component_warning = ledger_rules.check_components(entry.amount, entry.components)
        if component_warning:
            entry.warnings.append(component_warning)
        reimbursement = ledger_rules.reimbursement_note(entry.components)
        if reimbursement:
            entry.warnings.append(reimbursement)

    @staticmethod
    def _flag_duplicates_within_message(entries: list) -> None:
        """同一則訊息裡出現兩筆一樣的（同日、同金額、同類型、標題相近）。"""
        for index, entry in enumerate(entries):
            for earlier in entries[:index]:
                if (
                    entry.is_recordable
                    and entry.amount == earlier.amount
                    and entry.entry_type == earlier.entry_type
                    and entry.date == earlier.date
                    and ledger_rules.titles_similar(entry.title, earlier.title)
                ):
                    entry.warnings.append(
                        f"和同一則訊息裡的「{earlier.title}」看起來是同一筆，請確認是否重複"
                    )
                    break

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").strip()
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    # ── LLM → Rule fallback ──────────────────────────────

    def _parse_with_llm_fallback(self, text: str) -> ParsedMessage:
        if self.llm_parser.is_available():
            result = self.llm_parser.parse_multi(text)
            if result and result.get("entries"):
                entries = [
                    AccountEntry(
                        title=item["title"],
                        amount=float(item.get("amount", 0) or 0),
                        entry_type=item.get("type", "支出"),
                        category=item.get("category", "其他支出"),
                        date=item.get("date"),
                        period=item.get("period"),
                        vendor=item.get("vendor", ""),
                        payment_method=item.get("payment_method", "未指定"),
                        description=item.get("description", ""),
                        invoice_number=item.get("invoice_number", ""),
                        tax_amount=float(item.get("tax_amount", 0) or 0),
                        components=item.get("components") or [],
                        parse_engine="llm",
                        parse_confidence=0.95,
                    )
                    for item in result["entries"]
                    if item.get("title")
                ]
                if entries:
                    message = ParsedMessage(entries=entries)
                    # 單筆時備註保留原文，之後對帳才查得到當初怎麼寫的
                    if len(entries) == 1 and not entries[0].description and "\n" in text:
                        entries[0].description = text
                    total_warning = ledger_rules.check_stated_total(
                        sum(e.amount for e in entries), result.get("stated_total")
                    )
                    if total_warning and len(entries) > 1:
                        message.warnings.append(total_warning)
                    if result.get("truncated"):
                        message.warnings.append(
                            f"這則訊息的項目太多，只處理了前 {len(entries)} 筆，其餘請分開再傳一次"
                        )
                    return message
        return ParsedMessage(entries=[self._parse_freeform(text)])

    # ── Rule Engine ───────────────────────────────────────

    def _parse_freeform(self, text: str) -> AccountEntry:
        entry = AccountEntry(parse_engine="rule", parse_confidence=0.6)
        entry.date, date_token = ledger_rules.detect_date(text, local_now().date())
        # 先拿掉日期字樣再找金額，否則「10/4 勞保 11122」會把 10 當成金額
        body = text.replace(date_token, " ", 1) if date_token else text
        entry.amount = self._extract_amount(body)
        entry.entry_type = self._detect_type(text)
        entry.category = self._detect_category(text, entry.entry_type)
        entry.title = self._clean_title(body)
        entry.description = text
        return entry

    def _parse_structured(self, text: str) -> AccountEntry:
        """解析管道分隔格式：摘要: xxx | 金額: 1000 | 類型: 支出"""
        entry = AccountEntry(parse_engine="rule", parse_confidence=0.85)
        field_map = {
            "摘要": "title", "標題": "title", "title": "title",
            "金額": "amount", "amount": "amount",
            "類型": "entry_type", "type": "entry_type",
            "分類": "category", "category": "category",
            "日期": "date", "date": "date",
            "對象": "vendor", "vendor": "vendor",
            "付款方式": "payment_method",
            "發票號碼": "invoice_number",
        }
        for part in text.split("|"):
            sep = "：" if "：" in part else ":" if ":" in part else None
            if not sep:
                continue
            try:
                key, value = part.split(sep, 1)
            except ValueError:
                continue
            key = key.strip().lower()
            value = value.strip()
            mapped = field_map.get(key)
            if mapped == "title":
                entry.title = value
            elif mapped == "amount":
                entry.amount = self._parse_number(value)
            elif mapped == "entry_type":
                entry.entry_type = "收入" if "收" in value else "支出"
            elif mapped == "category":
                entry.category = value
            elif mapped == "date":
                entry.date = self._parse_date_str(value)
            elif mapped == "vendor":
                entry.vendor = value
            elif mapped == "payment_method":
                entry.payment_method = value
            elif mapped == "invoice_number":
                entry.invoice_number = value
        return entry

    def _parse_command(self, text: str) -> AccountEntry:
        """/收 學費 5000 | /支 講義 4500 | /record 買講義 1500"""
        entry = AccountEntry(parse_engine="rule", parse_confidence=0.9)
        if text.startswith("/收"):
            entry.entry_type = "收入"
            content = text[2:].strip()
        elif text.startswith("/支"):
            entry.entry_type = "支出"
            content = text[2:].strip()
        else:
            content = text[7:].strip()  # /record

        entry.date, date_token = ledger_rules.detect_date(content, local_now().date())
        if date_token:
            content = content.replace(date_token, " ", 1).strip()
        entry.amount = self._extract_amount(content)
        entry.title = self._clean_title(content)
        entry.category = self._detect_category(entry.title, entry.entry_type)
        return entry

    # ── 工具方法 ──────────────────────────────────────────

    def _extract_amount(self, text: str) -> float:
        """
        Bug #2 修正：NT$ 作為獨立 pattern，避免字元類別拆解問題。
        優先順序：NT$ → $＄ → 元/塊 → 裸數字（指令格式 fallback）
        """
        patterns = [
            r"NT\$\s*([\d,]+\.?\d*)",    # NT$4,500
            r"[\$＄]\s*([\d,]+\.?\d*)",   # $4,500 / ＄4,500
            r"([\d,]+\.?\d*)\s*[元塊]",   # 4500元 / 4500塊
            r"\b([\d,]+\.?\d*)\b",        # 裸數字（/支 講義 4500 等指令格式）
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return self._parse_number(match.group(1))
        return 0.0


    def _parse_number(self, s: str) -> float:
        try:
            return abs(float(s.replace(",", "")))
        except (ValueError, TypeError):
            return 0.0

    def _detect_type(self, text: str) -> str:
        """Bug #3 修正：使用完整詞組，避免「收」誤匹配「收據」等歧義。"""
        for phrase in INCOME_PHRASES:
            if phrase in text:
                return "收入"
        for phrase in EXPENSE_PHRASES:
            if phrase in text:
                return "支出"
        return "支出"

    def _detect_category(self, text: str, entry_type: str) -> str:
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return category
        return "其他收入" if entry_type == "收入" else "其他支出"

    def _detect_date(self, text: str) -> Optional[str]:
        return ledger_rules.detect_date(text, local_now().date())[0]

    def _parse_date_str(self, s: str) -> Optional[str]:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return self._detect_date(s)

    def _clean_title(self, text: str) -> str:
        """從原文取出簡短摘要：只取第一行、拿掉金額與裝飾符號。

        「3月薪資」「30 本」「第7期」這類數字是內容的一部分，要保留；
        只移除看起來是金額的數字（有 $／元，或前後沒有接文字的獨立數字）。
        """
        first_line = next((line for line in text.splitlines() if line.strip()), text)
        title = re.sub(r"(?:NT\$|[\$＄])\s*[\d,]+\.?\d*", "", first_line)
        title = re.sub(r"\d[\d,]*\.?\d*\s*[元塊]", "", title)
        title = re.sub(r"(?<![\w/.])\d[\d,]*(?:\.\d+)?(?![\w/.])", "", title)
        title = re.sub(r"[*_`#>]+", "", title)                      # Markdown 符號
        title = re.sub(r"[^\w\s\-+&/().（）、，,：:]", "", title)      # 表情符號等
        title = re.sub(r"\s+", " ", title).strip(" -:：,，、。.")
        return title[:50] if title else first_line.strip()[:50]

    def _build_ocr_title(self, ocr: dict) -> str:
        vendor = ocr.get("vendor", "")
        items = ocr.get("items", [])
        if items:
            first_item = items[0].get("name", "")
            suffix = f" 等 {len(items)} 項" if len(items) > 1 else ""
            return f"{vendor} - {first_item}{suffix}".strip(" -")
        return vendor or "發票/收據"

    def _classify_ocr(self, ocr: dict) -> str:
        suggested = ocr.get("category")
        if suggested in EXPENSE_CATEGORIES and suggested != "其他支出":
            return suggested
        all_text = ocr.get("raw_text", "")
        items_text = " ".join(item.get("name", "") for item in ocr.get("items", []))
        return self._detect_category(f"{all_text} {items_text}", "支出")
