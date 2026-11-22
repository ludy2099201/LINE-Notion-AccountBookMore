"""LLM 記帳解析引擎。"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

from config.settings import llm_config, local_today

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 2
BASE_BACKOFF_SECONDS = 1
# 一則訊息最多拆成幾筆（避免單一 webhook 寫入過久）
MAX_ENTRIES_PER_MESSAGE = 15
# 長訊息（薪資明細、多筆學費）需要較多輸出 token；
# 過小會讓 JSON 被截斷而整則訊息退回規則引擎。
MAX_OUTPUT_TOKENS = 4096

SYSTEM_PROMPT = """你是一個專業的補習班記帳助手。將使用者的訊息解析為結構化記帳資料。

請回傳 JSON 物件：{"entries": [ ... ], "stated_total": 數字或 null}

■ 拆帳規則
- 一則訊息若列出多筆各自獨立的收支（例如多位學生、多個課程、多張帳單），entries 內每筆各一個物件。
- 薪資明細（底薪、獎金、勞健保自付額、代墊款…加減後得到一個實付金額）只算「一筆」，amount 填實付總額，
  並把每個加減項放進 components。
- 「總共」「合計」「Total」這類加總行不是一筆交易，不要放進 entries；把該數字填入 stated_total。
  若訊息沒有寫出跨多筆的總額，stated_total 填 null。

■ 每筆 entries 的欄位
- title：15 字以內的簡短摘要，格式「對象 + 月份/期別 + 項目」，例如「Celia 3月薪資」「Liam 七月Workshop」「8月勞保」。
  不要包含金額、表情符號、Markdown 符號或算式。
- amount：正數，實際收付金額（折扣後、加減後的最終金額）。
- type：「收入」或「支出」。
  收到、繳費、入帳、學費、報名 → 收入；買、付、支付、花、購、修、租、薪資、匯給 → 支出。
  退費給學生或家長 → 支出；收到廠商退款 → 收入。
- category：必須是下列 14 項之一，且要與 type 相符。
  【收入類】學費收入、教材收入、報名費、其他收入
  【支出類】教材費、房租水電、人事費用、設備維護、辦公用品、餐飲點心、交通費、廣告行銷、保險稅務、其他支出
  固定歸類：
  · 課程、Workshop、營隊、學費（含早鳥價、折扣價）→ 學費收入（只有單獨收取的報名手續費才用「報名費」）
  · 薪資、薪水、獎金、bonus、鐘點費、勞退 → 人事費用
  · 勞保、健保、災保、工會勞健保、各種稅 → 保險稅務
  · 房租、租金、管理費、水費、電費、網路 → 房租水電
  · 飲料、點心、便當、餐費、食材 → 餐飲點心
- date：實際收付日期 YYYY-MM-DD。只有訊息明確提到日期（今天、昨天、10/4、上週五…）才填，否則填 null。
  不要把「8月勞保」「三月薪資」這種歸屬月份當成日期。
- period：這筆帳歸屬的月份 YYYY-MM（例如「8月勞保」→ 當年 08 月、「三月薪資」→ 03 月）。
  訊息有寫年份就照寫；沒寫年份時，支出取最近一次已到來的該月份（最晚到下個月），
  收入若該月份落在未來 6 個月內取未來、否則取過去。訊息沒有提到歸屬月份就填 null。
- vendor：收付款對象。收入填學生/家長姓名，薪資填老師姓名，其餘填店家或機構；看不出來填 ""。
- payment_method：現金、刷卡、轉帳、LINE Pay、未指定 之一（訊息沒提就填「未指定」）。
- description：補充說明（折扣原因、品項、算式等），沒有就填 ""。不要只是重複 title。
- invoice_number：發票號碼，沒有填 ""。
- tax_amount：稅額數字，沒有填 0。
- components：只有 amount 是由多個加減項算出來時才填，
  格式 [{"label": "底薪", "amount": 42750}, {"label": "勞保自付額", "amount": -738}]，
  加項為正、減項為負，照訊息原文的數字填，不要自行修正。其他情況填 []。

請只回傳 JSON，不要包含 markdown。"""

INCOME_CATEGORIES = frozenset({"學費收入", "教材收入", "報名費", "其他收入"})
EXPENSE_CATEGORIES = frozenset({
    "教材費", "房租水電", "人事費用", "設備維護",
    "辦公用品", "餐飲點心", "交通費", "廣告行銷",
    "保險稅務", "其他支出",
})
VALID_CATEGORIES = INCOME_CATEGORIES | EXPENSE_CATEGORIES

VALID_PAYMENT_METHODS = frozenset({
    "現金", "刷卡", "轉帳", "LINE Pay", "未指定"
})

_PERIOD_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")


def normalize_payment_method(value: object) -> str:
    """把任意付款方式字串收斂到 Notion 既有的 5 個選項。

    Notion 的 select 欄位遇到沒看過的值會自動新增選項，
    所以「未知」「信用卡」這類值必須在寫入前擋下來。
    """
    text = str(value or "").strip()
    if not text:
        return "未指定"
    lowered = text.lower().replace(" ", "")
    if "line" in lowered and "pay" in lowered:
        return "LINE Pay"
    if text in VALID_PAYMENT_METHODS:
        return text
    aliases = {
        "現金": ("現金", "cash", "付現"),
        "刷卡": ("刷卡", "信用卡", "credit", "visa", "master"),
        "轉帳": ("轉帳", "匯款", "匯入", "atm", "transfer", "銀行"),
    }
    for canonical, keywords in aliases.items():
        if any(keyword in lowered for keyword in keywords):
            return canonical
    return "未指定"


def normalize_date(value: object) -> Optional[str]:
    """將日期字串正規化為 YYYY-MM-DD；無法解析則回傳 None。"""
    if not value:
        return None
    text = str(value).strip().replace("/", "-").replace(".", "-")
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_period(value: object) -> Optional[str]:
    """將歸屬月份正規化為 YYYY-MM；無法解析則回傳 None。"""
    if not value:
        return None
    match = _PERIOD_RE.match(str(value).strip()[:7])
    if not match:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{match.group(1)}-{month:02d}"


def _to_float(value: object) -> float:
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    try:
        return float(value) if value else 0.0
    except (TypeError, ValueError):
        return 0.0


class LLMParser:
    """將自然語言記帳訊息解析為結構化資料。"""

    def __init__(self) -> None:
        self.api_key = llm_config.api_key
        self.model = llm_config.model
        self.enabled = bool(self.api_key)

        if self.enabled:
            logger.info("LLM Parser initialized with model: %s", self.model)
        else:
            logger.warning("LLM Parser disabled: GEMINI_API_KEY not configured.")

    def _post_with_retry(self, payload: dict) -> Optional[dict]:
        """向 Gemini API 發送請求，失敗時指數退避重試（最多 MAX_RETRIES 次）。"""
        if not self.api_key:
            return None

        url = f"{GEMINI_BASE}/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        params = {"key": self.api_key}

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    params=params,
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                is_last_attempt = attempt == MAX_RETRIES
                logger.warning(
                    "LLM request failed on attempt %d: %s", attempt + 1, exc
                )
                if is_last_attempt:
                    logger.error("LLM API failed after %d retries: %s", MAX_RETRIES, exc)
                    return None
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))

        return None

    def parse(self, text: str) -> Optional[dict]:
        """
        將自然語言文字解析為單筆結構化記帳 dict（取第一筆）。

        保留給只需要單筆結果的呼叫端；一則訊息多筆請用 parse_multi()。
        """
        result = self.parse_multi(text)
        if not result or not result["entries"]:
            return None
        return result["entries"][0]

    def parse_multi(self, text: str) -> Optional[dict]:
        """
        將自然語言文字解析為一或多筆記帳資料。

        Args:
            text: 使用者輸入的記帳訊息

        Returns:
            {"entries": [已驗證的 dict, ...], "stated_total": float | None}，
            或 None（解析失敗 / 沒有任何有效的一筆）
        """
        if not self.enabled:
            return None

        today = local_today()
        full_prompt = f"{SYSTEM_PROMPT}\n今天日期：{today}\n\n使用者訊息：{text}"

        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
            },
        }

        data = self._post_with_retry(payload)
        if not data:
            return None

        try:
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            finish_reason = candidates[0].get("finishReason")
            if finish_reason and finish_reason != "STOP":
                logger.warning("LLM finished with reason: %s", finish_reason)
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None

            text_result = parts[0].get("text", "").strip()
            if text_result.startswith("```"):
                lines = text_result.split("\n")
                text_result = "\n".join(lines[1:-1])

            parsed = json.loads(text_result)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse LLM JSON: %s", exc)
            return None

        # 相容三種回傳形狀：{"entries": [...]}、[...]、單一物件
        stated_total = None
        if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
            raw_entries = parsed["entries"]
            stated_total = _to_float(parsed.get("stated_total")) or None
        elif isinstance(parsed, list):
            raw_entries = parsed
        elif isinstance(parsed, dict):
            raw_entries = [parsed]
        else:
            return None

        entries = []
        for raw in raw_entries[:MAX_ENTRIES_PER_MESSAGE]:
            if not isinstance(raw, dict):
                continue
            validated = self._validate_result(raw)
            if validated:
                entries.append(validated)
        if not entries:
            return None

        return {
            "entries": entries,
            "stated_total": stated_total,
            "truncated": len(raw_entries) > MAX_ENTRIES_PER_MESSAGE,
        }

    def _validate_result(self, parsed: dict) -> Optional[dict]:
        """驗證並正規化 LLM 回傳的記帳資料。"""
        title = str(parsed.get("title") or "").strip()
        if not title:
            return None

        amount = abs(_to_float(parsed.get("amount")))

        entry_type = parsed.get("type", "支出")
        if entry_type not in {"收入", "支出"}:
            entry_type = "支出"

        category = parsed.get("category", "其他支出")
        if category not in VALID_CATEGORIES:
            category = "其他收入" if entry_type == "收入" else "其他支出"

        components = []
        raw_components = parsed.get("components")
        if isinstance(raw_components, list):
            for item in raw_components:
                if not isinstance(item, dict):
                    continue
                value = _to_float(item.get("amount"))
                if value:
                    components.append({
                        "label": str(item.get("label") or "").strip(),
                        "amount": value,
                    })

        return {
            "title": title,
            "amount": amount,
            "type": entry_type,
            "category": category,
            "date": normalize_date(parsed.get("date")),
            "period": normalize_period(parsed.get("period")),
            "vendor": str(parsed.get("vendor") or "").strip(),
            "payment_method": normalize_payment_method(parsed.get("payment_method")),
            "description": str(parsed.get("description") or "").strip(),
            "invoice_number": str(parsed.get("invoice_number") or "").strip(),
            "tax_amount": abs(_to_float(parsed.get("tax_amount"))),
            "components": components,
        }

    def is_available(self) -> bool:
        return self.enabled
