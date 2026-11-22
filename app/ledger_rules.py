"""記帳資料品質規則（純函式，不呼叫任何外部服務）。

這些規則來自實際帳本裡反覆出現的問題：
- 同一種費用被記到不同分類（管理費、Workshop）
- 「日期」填成登記日，看不出這筆帳屬於哪個月
- 同一筆被記兩次
- 修正訊息、測試訊息被當成新的一筆寫進正式帳
- 薪資明細加總與實付金額對不起來
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.llm_parser import EXPENSE_CATEGORIES, INCOME_CATEGORIES

# ── 分類固定規則 ──────────────────────────────────────
# 依序比對，第一個命中的規則生效。順序有意義：
# 「Mia 4月薪資（含勞健保自付額）」要先命中薪資，而不是勞健保。
_EXPENSE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"薪資|薪水|獎金|bonus|鐘點費|工讀", re.IGNORECASE), "人事費用"),
    (re.compile(r"勞退"), "人事費用"),
    (re.compile(
        r"勞保|健保|災保|勞健保|印花稅|營業稅|所得稅|房屋稅|地價稅|牌照稅|稅金|稅款|繳稅|報稅"
    ), "保險稅務"),
    (re.compile(r"管理費|房租|租金|水費|電費|水電"), "房租水電"),
]
_INCOME_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"workshop|課程|營隊|學費|補習費", re.IGNORECASE), "學費收入"),
]

_EMPTY_VALUES = frozenset({"", "未指定", "未知", "無", "不明", "n/a", "na", "none", "null", "-"})

_CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
# 月份前面可以帶年份：西元（2025年8月）或民國（114年8月）
_PERIOD_YEAR = r"(?:(?<!\d)(\d{3,4})\s*年\s*)?"
_PERIOD_DIGIT_RE = re.compile(
    _PERIOD_YEAR + r"(?<![\d/.\-])(\d{1,2})\s*月(?:份)?(?!\s*\d{1,2}\s*[日號])"
)
_PERIOD_CN_RE = re.compile(_PERIOD_YEAR + r"(十[一二]?|[一二三四五六七八九])月")

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")
_MD_SLASH_RE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/])")
_MD_CN_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]")

_CORRECTION_RE = re.compile(
    r"^\s*(請修正|請更正|請改成|請改為|幫我修正|幫我更正|幫我改|(修正|更正)\s*[:：]|(修正|更正)\s)"
)
_DRY_RUN_PREFIX_RE = re.compile(r"^\s*(/test\b|測試\s*[:：]|測試\s+|測試$)", re.IGNORECASE)
_DRY_RUN_ANYWHERE_RE = re.compile(r"測試寫入|網站測試|系統測試|測試一下|測試用|bot\s*測試", re.IGNORECASE)

UNDO_COMMANDS = frozenset({"/undo", "undo", "刪除上一筆", "撤銷", "撤銷上一筆", "取消上一筆", "刪掉上一筆"})

# 明細加總允許的誤差（元）
COMPONENT_TOLERANCE = 0.5


# ── 欄位正規化 ────────────────────────────────────────

def clean_optional(value: object) -> str:
    """把「未指定」「未知」這類佔位字當成沒填，回傳空字串。"""
    text = str(value or "").strip()
    return "" if text.lower() in _EMPTY_VALUES else text


def canonical_category(title: str, entry_type: str, category: str) -> tuple[str, Optional[str]]:
    """套用固定分類規則並確保分類與收支類型一致。

    Returns:
        (分類, 提醒文字或 None)
    """
    rules = _INCOME_RULES if entry_type == "收入" else _EXPENSE_RULES
    for pattern, target in rules:
        if pattern.search(title or ""):
            return target, None

    allowed = INCOME_CATEGORIES if entry_type == "收入" else EXPENSE_CATEGORIES
    if category in allowed:
        return category, None

    fallback = "其他收入" if entry_type == "收入" else "其他支出"
    if category in INCOME_CATEGORIES | EXPENSE_CATEGORIES:
        return fallback, (
            f"分類「{category}」和收支類型「{entry_type}」對不上，已改記為「{fallback}」，請確認是收入還是支出"
        )
    return fallback, None


# ── 日期與歸屬月份 ────────────────────────────────────

# 沒寫年份時，歸屬月份最多可以落在未來幾個月（超過就視為過去的月份）
PERIOD_MONTHS_AHEAD = {"收入": 6, "支出": 1}


def _period_year(month: int, today: date, months_ahead: int) -> int:
    """沒寫年份時決定歸屬月份的年份。

    支出多半是補記已發生的月份（11 月記「3月薪資」是今年 3 月）；
    收入常是預收（4 月收「七月Workshop」是今年 7 月）。
    """
    ahead = (month - today.month) % 12          # 往未來數要幾個月（0–11）
    offset = ahead if ahead <= months_ahead else ahead - 12
    return today.year + (today.month - 1 + offset) // 12


def _explicit_year(raw: Optional[str]) -> Optional[int]:
    """把訊息裡寫的年份轉成西元年；三位數視為民國年。看不懂就回傳 None。"""
    if not raw:
        return None
    year = int(raw) + 1911 if len(raw) == 3 else int(raw)
    return year if 2000 <= year <= 2100 else None


def detect_period(text: str, today: date, entry_type: str = "支出") -> Optional[str]:
    """從文字找出歸屬月份（「8月勞保」「七月Workshop」），回傳 YYYY-MM。

    「10月4日」是日期不是歸屬月份，不會被當成歸屬月份。
    """
    if not text:
        return None
    month = None
    match = _PERIOD_DIGIT_RE.search(text)
    if match and 1 <= int(match.group(2)) <= 12:
        month = int(match.group(2))
    else:
        match = _PERIOD_CN_RE.search(text)
        if match:
            month = _CN_MONTHS[match.group(2)]
    if month is None:
        return None
    # 訊息寫了年份就照寫的年份，沒寫才用今天推算
    year = _explicit_year(match.group(1)) or _period_year(
        month, today, PERIOD_MONTHS_AHEAD.get(entry_type, 1)
    )
    return f"{year}-{month:02d}"


def detect_date(text: str, today: date) -> tuple[Optional[str], Optional[str]]:
    """從文字找出實際收付日期。

    Returns:
        (YYYY-MM-DD 或 None, 命中的原文片段或 None)
    """
    if not text:
        return None, None
    for keyword, delta in (("今天", 0), ("昨天", -1), ("前天", -2)):
        if keyword in text:
            return (today + timedelta(days=delta)).strftime("%Y-%m-%d"), keyword

    match = _ISO_DATE_RE.search(text)
    if match:
        try:
            parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return parsed.strftime("%Y-%m-%d"), match.group(0)
        except ValueError:
            pass

    for pattern in (_MD_CN_RE, _MD_SLASH_RE):
        match = pattern.search(text)
        if not match:
            continue
        month, day = int(match.group(1)), int(match.group(2))
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            continue
        # 沒寫年份：預設今年；若落在兩個月以後，視為去年的日期
        if candidate > today + timedelta(days=60):
            try:
                candidate = date(today.year - 1, month, day)
            except ValueError:
                continue
        return candidate.strftime("%Y-%m-%d"), match.group(0)

    return None, None


# ── 訊息類型判斷 ──────────────────────────────────────

def is_correction_message(text: str) -> bool:
    """「請修正 這個是收入…」這類訊息是要改舊資料，不該新增一筆。"""
    return bool(_CORRECTION_RE.match(text or ""))


def is_dry_run_message(text: str) -> bool:
    """測試訊息只預覽、不寫入正式帳。「測試卷印刷 300」這種正常支出不算。"""
    text = text or ""
    return bool(_DRY_RUN_PREFIX_RE.match(text) or _DRY_RUN_ANYWHERE_RE.search(text))


def strip_dry_run_prefix(text: str) -> str:
    return _DRY_RUN_PREFIX_RE.sub("", text or "", count=1).strip()


def is_undo_command(text: str) -> bool:
    return (text or "").strip().lower() in UNDO_COMMANDS


# ── 重複偵測 ──────────────────────────────────────────

def title_key(title: str) -> str:
    """標題比對用的正規化字串：去空白、標點、括號符號，轉小寫。"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


def titles_similar(a: str, b: str) -> bool:
    """標題相同，或一個包含另一個（「飲料」與「飲料（特價）」）。"""
    key_a, key_b = title_key(a), title_key(b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    shorter, longer = sorted((key_a, key_b), key=len)
    return len(shorter) >= 2 and shorter in longer


# ── 金額檢核 ──────────────────────────────────────────

def check_components(amount: float, components: list) -> Optional[str]:
    """薪資這類「加減後得出實付金額」的訊息：明細加總要等於金額。"""
    if not components or len(components) < 2:
        return None
    total = sum(float(item.get("amount", 0) or 0) for item in components)
    diff = total - amount
    if abs(diff) <= COMPONENT_TOLERANCE:
        return None
    formula = " ".join(
        f"{'+' if item['amount'] >= 0 else '−'}{abs(item['amount']):,.0f}"
        for item in components
    ).lstrip("+")
    return (
        f"明細加總 {formula} = {total:,.0f}，和記錄金額 {amount:,.0f} 差 {abs(diff):,.0f} 元，請確認哪個才對"
    )


def reimbursement_note(components: list) -> Optional[str]:
    """薪資內含代墊款時，提醒不要再另外記一筆代墊支出。"""
    for item in components or []:
        if "代墊" in str(item.get("label", "")) and float(item.get("amount", 0) or 0) > 0:
            return (
                f"這筆已包含代墊款 {item['amount']:,.0f} 元；"
                "如果該筆代墊已另外記過支出，請刪掉其中一筆以免重複計算"
            )
    return None


def check_stated_total(entries_total: float, stated_total: Optional[float]) -> Optional[str]:
    """訊息寫了「總共 $121,500」時，各筆加總要等於它。"""
    if not stated_total:
        return None
    diff = entries_total - stated_total
    if abs(diff) <= COMPONENT_TOLERANCE:
        return None
    return (
        f"各筆加總 {entries_total:,.0f} 和訊息寫的總額 {stated_total:,.0f} 差 {abs(diff):,.0f} 元，"
        "可能有漏記或多記"
    )
