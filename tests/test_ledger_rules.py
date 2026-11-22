"""記帳資料品質規則測試。案例取自實際帳本中出現過的問題。"""

from datetime import date

import pytest

from app import ledger_rules as rules

TODAY = date(2026, 11, 22)


# ── 分類固定規則 ──────────────────────────────────────

@pytest.mark.parametrize("title, entry_type, category, expected", [
    ("7月管理費", "支出", "其他支出", "房租水電"),          # 曾被記成其他支出
    ("Lyanna七月Workshop", "收入", "報名費", "學費收入"),    # 曾被記成報名費
    ("Mia 4月薪資(含勞健保自付額)", "支出", "保險稅務", "人事費用"),
    ("勞退休金", "支出", "保險稅務", "人事費用"),
    ("老師工會4月勞健保", "支出", "其他支出", "保險稅務"),
    ("印花稅", "支出", "其他支出", "保險稅務"),
    ("教育局罰款", "支出", "其他支出", "其他支出"),
    ("墊腳石買文具", "支出", "辦公用品", "辦公用品"),
])
def test_canonical_category(title, entry_type, category, expected):
    assert rules.canonical_category(title, entry_type, category) == (expected, None)


def test_canonical_category_type_mismatch_warns():
    category, warning = rules.canonical_category("王小明退費", "支出", "學費收入")
    assert category == "其他支出"
    assert "對不上" in warning


def test_canonical_category_unknown_value_falls_back_silently():
    assert rules.canonical_category("雜項", "收入", "亂填") == ("其他收入", None)


def test_clean_optional_treats_placeholders_as_empty():
    assert rules.clean_optional("未指定") == ""
    assert rules.clean_optional("未知") == ""
    assert rules.clean_optional(None) == ""
    assert rules.clean_optional(" Celia ") == "Celia"


# ── 歸屬月份 ──────────────────────────────────────────

@pytest.mark.parametrize("text, today, entry_type, expected", [
    ("8月勞保", date(2026, 10, 4), "支出", "2026-08"),        # 10 月補記 8 月
    ("勞保+災保七月", date(2026, 10, 4), "支出", "2026-07"),
    ("Celia 3月薪資", TODAY, "支出", "2026-03"),              # 不可跳到明年 3 月
    ("12月薪資", date(2027, 1, 5), "支出", "2026-12"),        # 跨年補記
    ("12月份房租", TODAY, "支出", "2026-12"),                 # 預付下個月
    ("Liam 七月Workshop", date(2026, 4, 11), "收入", "2026-07"),  # 預收
    ("11月管理費", TODAY, "支出", "2026-11"),
    ("2025年8月勞保", TODAY, "支出", "2025-08"),              # 寫了年份就照寫的
    ("2025 年 12 月份房租", TODAY, "支出", "2025-12"),
    ("114年8月勞保", TODAY, "支出", "2025-08"),               # 民國年
    ("2027年一月Workshop", TODAY, "收入", "2027-01"),
    ("9999年8月勞保", TODAY, "支出", "2026-08"),              # 不合理的年份不採用
])
def test_detect_period(text, today, entry_type, expected):
    assert rules.detect_period(text, today, entry_type) == expected


@pytest.mark.parametrize("text", [
    "10月4日 勞保",        # 這是日期
    "教育局罰款第12期",
    "買了3個月的網路",
    "115年03-04月",        # 發票期別
    "房租",
])
def test_detect_period_ignores_non_periods(text):
    assert rules.detect_period(text, TODAY) is None


# ── 日期 ──────────────────────────────────────────────

@pytest.mark.parametrize("text, expected, token", [
    ("10/4 勞保 11122", "2026-10-04", "10/4"),
    ("10月4日付勞保", "2026-10-04", "10月4日"),
    ("2026/3/14 珍煮丹", "2026-03-14", "2026/3/14"),
    ("昨天買講義", "2026-11-21", "昨天"),
    ("12/30 房租", "2026-12-30", "12/30"),
    ("3/5 文具", "2026-03-05", "3/5"),
])
def test_detect_date(text, expected, token):
    assert rules.detect_date(text, TODAY) == (expected, token)


def test_detect_date_far_future_means_last_year():
    assert rules.detect_date("12/30 房租", date(2027, 1, 5))[0] == "2026-12-30"


@pytest.mark.parametrize("text", ["6500*2=13000", "8月勞保 11122", "買講義 500", "13/45 亂碼"])
def test_detect_date_none(text):
    assert rules.detect_date(text, TODAY) == (None, None)


# ── 訊息類型 ──────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("請修正 這個是收入：- Liam 七月Workshop", True),   # 曾被當成新的一筆寫入
    ("更正：金額是 500", True),
    ("修正液 50", False),
    ("修正帶 30 元", False),
    ("買講義 500", False),
])
def test_is_correction_message(text, expected):
    assert rules.is_correction_message(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("測試 買講義 500", True),
    ("/test 買講義 500", True),
    ("收入 500 備註 網站測試", True),
    ("測試", True),
    ("測試卷印刷 300", False),      # 正常支出
    ("入班測試費 300", False),
])
def test_is_dry_run_message(text, expected):
    assert rules.is_dry_run_message(text) is expected


def test_strip_dry_run_prefix():
    assert rules.strip_dry_run_prefix("測試 買講義 500") == "買講義 500"
    assert rules.strip_dry_run_prefix("/test 買講義 500") == "買講義 500"
    assert rules.strip_dry_run_prefix("測試") == ""


@pytest.mark.parametrize("text", ["刪除上一筆", "/undo", "UNDO", " 撤銷 "])
def test_is_undo_command(text):
    assert rules.is_undo_command(text)


def test_is_undo_command_rejects_normal_text():
    assert not rules.is_undo_command("刪除上一筆的講義 500")


# ── 重複偵測 ──────────────────────────────────────────

@pytest.mark.parametrize("a, b, expected", [
    ("Jennie Workshop 早鳥優惠價", "jennie workshop早鳥優惠價", True),
    ("飲料", "飲料（特價）", True),
    ("Liam 秋季課程", "Lyanna 秋季課程", False),       # 不同學生同金額不算重複
    ("Kurt 暑期常態課程", "Matt 暑期常態課程", False),
    ("", "飲料", False),
])
def test_titles_similar(a, b, expected):
    assert rules.titles_similar(a, b) is expected


# ── 金額檢核 ──────────────────────────────────────────

CELIA = [
    {"label": "底薪", "amount": 42750},
    {"label": "勞保自付額", "amount": -738},
    {"label": "健保自付額", "amount": -458},
    {"label": "其他扣款", "amount": -199},
    {"label": "代墊款項", "amount": 3869},
]


def test_check_components_flags_mismatch():
    """Celia 3 月薪資：明細加總 45,224，但記錄金額 45,214。"""
    warning = rules.check_components(45214, CELIA)
    assert "45,224" in warning and "45,214" in warning and "差 10 元" in warning


def test_check_components_ok_when_matching():
    assert rules.check_components(45224, CELIA) is None
    assert rules.check_components(500, []) is None
    assert rules.check_components(500, [{"label": "x", "amount": 400}]) is None


def test_reimbursement_note():
    assert "3,869" in rules.reimbursement_note(CELIA)
    assert rules.reimbursement_note(CELIA[:4]) is None


def test_check_stated_total():
    assert rules.check_stated_total(121500, 121500) is None
    assert rules.check_stated_total(121500, None) is None
    assert "差 15,300 元" in rules.check_stated_total(106200, 121500)
