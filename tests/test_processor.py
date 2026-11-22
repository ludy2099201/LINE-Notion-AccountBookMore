"""
Data Processor 整合與單元測試（Task #11 — 優化 ⑩）。

使用 pytest + monkeypatch，涵蓋以下情境：
- Bug #2：NT$ 等各種金額正則解析
- Bug #3：收支詞組判斷與避免誤判
- 自由文字 / 結構化 / 指令格式解析
- 相對日期解析
- LLM 失敗時 fallback 到 Rule Engine
- format_confirmation 輸出
"""

import pytest
from unittest.mock import MagicMock

from app.data_processor import AccountEntry, DataProcessor


@pytest.fixture
def processor():
    return DataProcessor()


# ── 金額提取與正則（Bug #2 修正）─────────────────────

def test_extract_amount_nt_dollar(processor):
    assert processor._extract_amount("NT$4,500") == 4500.0
    assert processor._extract_amount("NT$1200") == 1200.0


def test_extract_amount_dollar_sign(processor):
    assert processor._extract_amount("$1,200") == 1200.0


def test_extract_amount_fullwidth_dollar(processor):
    assert processor._extract_amount("＄300") == 300.0


def test_extract_amount_yuan_suffix(processor):
    assert processor._extract_amount("1200元") == 1200.0


def test_extract_amount_kuai_suffix(processor):
    assert processor._extract_amount("300塊") == 300.0


def test_extract_amount_bare_number(processor):
    """測試裸數字 (指令 fallback)"""
    assert processor._extract_amount("4500") == 4500.0


def test_extract_amount_no_amount(processor):
    assert processor._extract_amount("買了講義") == 0.0


def test_extract_amount_decimal(processor):
    assert processor._extract_amount("NT$10,000.5") == 10000.5


# ── 收支與分類判斷（Bug #3 修正）────────────────────

@pytest.mark.parametrize("text, expected", [
    ("收到學費 5000", "收入"),
    ("入帳五千", "收入"),
    ("退款處理完成", "收入"),
    ("繳費成功", "收入"),
    ("買了便當", "支出"),
    ("付房租", "支出"),
    ("購買影印紙", "支出"),
])
def test_detect_type_phrases(processor, text, expected):
    """測試完整詞組辨識，避免單字歧義。"""
    assert processor._detect_type(text) == expected


def test_detect_type_avoids_receipt_false_positive(processor, monkeypatch):
    """測試「收據」不該被誤判為「收入」。"""
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: False)
    entry = processor.process_text("收據一張，買文具 200 元")
    assert entry.entry_type == "支出"


@pytest.mark.parametrize("text, entry_type, expected", [
    ("影印紙 100", "支出", "辦公用品"),
    ("講義 500", "支出", "教材費"),
    ("水電費 2000", "支出", "房租水電"),
    ("工讀生薪資", "支出", "人事費用"),
    ("收到學費 5000", "收入", "學費收入"),
    ("活動報名費", "收入", "報名費"),
    ("未知支出", "支出", "其他支出"),
    ("未知收入", "收入", "其他收入"),
])
def test_detect_category(processor, text, entry_type, expected):
    """分類關鍵字自動對應與預設值測試。"""
    assert processor._detect_category(text, entry_type) == expected


def test_relative_date_detection(processor, monkeypatch):
    """測試昨天、今天等相對日期。"""
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: False)
    entry = processor.process_text("昨天買講義 500 元")
    assert entry.date is not None


# ── 輸入格式路由 ──────────────────────────────────────

def test_process_freeform_text_with_nt_amount(processor, monkeypatch):
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: False)
    entry = processor.process_text("買了 30 本數學講義 NT$4,500")
    assert entry.amount == 4500
    assert entry.entry_type == "支出"
    assert entry.category == "教材費"
    assert entry.parse_engine == "rule"


def test_process_structured_text(processor):
    entry = processor.process_text("摘要: 講義 | 金額: 4500 | 類型: 支出 | 分類: 教材費")
    assert entry.title == "講義"
    assert entry.amount == 4500
    assert entry.entry_type == "支出"
    assert entry.category == "教材費"
    assert entry.parse_engine == "rule"


def test_process_command_expense(processor):
    entry = processor.process_text("/支 講義 4500")
    assert entry.entry_type == "支出"
    assert entry.amount == 4500.0
    assert entry.parse_engine == "rule"


def test_process_command_income(processor):
    entry = processor.process_text("/收 王小明學費 12000")
    assert entry.entry_type == "收入"
    assert entry.amount == 12000.0
    assert entry.parse_engine == "rule"


def test_process_empty_text(processor):
    entry = processor.process_text("")
    assert entry.title == ""
    assert entry.amount == 0.0


# ── LLM 降級機制 ─────────────────────────────────────

def test_rule_engine_fallback_when_llm_returns_none(processor, monkeypatch):
    """LLM 回傳 None 時（例如解析失敗），應退回 Rule Engine。"""
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: True)
    monkeypatch.setattr(processor.llm_parser, "parse", lambda text: None)
    entry = processor.process_text("買影印紙 300 元")
    
    assert entry.parse_engine == "rule"
    assert entry.amount == 300.0
    assert entry.entry_type == "支出"
    assert entry.category == "辦公用品"


# ── 圖片 / OCR 路徑 ───────────────────────────────────

def test_process_image_ocr_disabled(processor, monkeypatch):
    """OCR 引擎未啟用時應回傳失敗 entry。"""
    monkeypatch.setattr(processor.ocr_engine, "is_available", lambda: False)
    entry = processor.process_image(b"fake")
    assert entry.title == "OCR 辨識失敗"
    assert entry.parse_engine == "ocr"


def test_process_image_ocr_success(processor, monkeypatch):
    """OCR 成功時應正確對應欄位。"""
    monkeypatch.setattr(processor.ocr_engine, "is_available", lambda: True)
    mock_recognize = MagicMock(return_value={
        "vendor": "7-11",
        "total_amount": 150,
        "confidence": 0.95,
        "items": [{"name": "飲料", "amount": 150}],
        "raw_text": "7-11 飲料 150",
    })
    monkeypatch.setattr(processor.ocr_engine, "recognize", mock_recognize)
    
    entry = processor.process_image(b"fake")
    assert entry.vendor == "7-11"
    assert entry.amount == 150.0
    assert entry.parse_engine == "ocr"


# ── 輸出格式 ──────────────────────────────────────────

def test_format_confirmation(processor):
    entry = AccountEntry(
        title="講義採購",
        amount=4500,
        entry_type="支出",
        category="教材費",
        parse_engine="rule",
        parse_confidence=0.9,
    )
    message = processor.format_confirmation(entry)
    
    assert "💸" in message
    assert "講義採購" in message
    assert "教材費" in message
    assert "$4,500" in message
    assert "規則引擎解析" in message


def test_format_confirmation_income(processor):
    entry = AccountEntry(
        title="學費",
        amount=12000,
        entry_type="收入",
        category="學費收入",
        parse_engine="llm",
        parse_confidence=0.95,
    )
    message = processor.format_confirmation(entry)
    
    assert "💰" in message
    assert "AI 智慧解析" in message
    assert "學費" in message
    assert "$12,000" in message


# ── 共用後處理（日期 / 歸屬月份 / 分類 / 付款方式）──────

@pytest.fixture
def rule_processor(processor, monkeypatch):
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: False)
    return processor


def test_date_defaults_to_today_and_is_flagged(rule_processor):
    entry = rule_processor.process_text("買影印紙 300 元")
    assert entry.date_defaulted is True
    assert entry.period == entry.date[:7]


def test_explicit_date_is_not_flagged_and_not_used_as_amount(rule_processor):
    """「10/4 勞保 11122」：10/4 是日期，金額是 11122（不是 10）。"""
    entry = rule_processor.process_text("10/4 勞保 11122")
    assert entry.amount == 11122
    assert entry.date.endswith("-10-04")
    assert entry.date_defaulted is False
    assert entry.category == "保險稅務"


def test_period_detected_from_title(rule_processor):
    entry = rule_processor.process_text("/支 8月勞退 3540")
    assert entry.title == "8月勞退"          # 月份數字不會被當成金額刪掉
    assert entry.period.endswith("-08")
    assert entry.category == "人事費用"


def test_management_fee_category_is_consistent(rule_processor):
    assert rule_processor.process_text("/支 7月管理費 11147").category == "房租水電"


def test_structured_unknown_payment_and_category_are_normalized(rule_processor):
    """結構化輸入的值直接寫進 Notion select 會新增選項，必須先收斂。"""
    entry = rule_processor.process_text(
        "摘要: 珍煮丹 | 金額: 417 | 類型: 支出 | 分類: 飲料類 | 付款方式: 未知 | 對象: 未指定"
    )
    assert entry.category == "其他支出"
    assert entry.payment_method == "未指定"
    assert entry.vendor == ""


def test_rule_title_uses_first_line_and_keeps_month(rule_processor):
    """帳本裡規則引擎曾把整段薪資明細塞進摘要、還刪掉「3月」的 3。"""
    entry = rule_processor.process_text(
        "Celia 3月薪資Total: 45214\n💰 42750\n\n➖ **勞保自付額：** −738（勞保金額）"
    )
    assert entry.title == "Celia 3月薪資Total"
    assert entry.amount == 45214
    assert entry.period.endswith("-03")


def test_ocr_payment_and_category_are_normalized(processor, monkeypatch):
    """珍煮丹發票：付款方式「未知」→ 未指定；分類採用 OCR 判斷的餐飲點心。"""
    monkeypatch.setattr(processor.ocr_engine, "is_available", lambda: True)
    monkeypatch.setattr(processor.ocr_engine, "recognize", lambda image: {
        "vendor": "珍煮丹 TRUEDAN", "total_amount": 417, "tax_amount": None,
        "payment_method": "未知", "category": "餐飲點心", "date": "2026-03-14",
        "invoice_number": "ZJ-38461657", "items": None,
        "raw_text": "珍煮丹 電子發票證明聯 115年03-04月 總計: 417", "confidence": 0.9,
    })
    entry = processor.process_image(b"fake")
    assert entry.payment_method == "未指定"
    assert entry.category == "餐飲點心"
    assert entry.date == "2026-03-14" and entry.date_defaulted is False
    assert entry.period == "2026-03"          # 跟著交易日，不從發票期別猜
    assert entry.is_recordable


def test_ocr_without_amount_is_not_recordable(processor, monkeypatch):
    monkeypatch.setattr(processor.ocr_engine, "is_available", lambda: True)
    monkeypatch.setattr(processor.ocr_engine, "recognize", lambda image: {
        "vendor": "某店", "total_amount": None, "confidence": 0.8, "raw_text": "",
    })
    assert processor.process_image(b"fake").is_recordable is False


def test_llm_description_repeating_title_is_dropped(processor, monkeypatch):
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: True)
    monkeypatch.setattr(processor.llm_parser, "parse_multi", lambda text: {
        "entries": [{"title": "Mia 薪水", "amount": 28304, "type": "支出",
                     "category": "人事費用", "description": "Mia薪水"}],
        "stated_total": None,
    })
    entry = processor.process_text("Mia 薪水 28304")
    assert entry.description == ""
    assert entry.parse_engine == "llm"


def test_duplicate_within_one_message_is_flagged(processor, monkeypatch):
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: True)
    item = {"title": "Jennie Workshop 早鳥優惠價", "amount": 15300, "type": "收入", "category": "學費收入"}
    monkeypatch.setattr(processor.llm_parser, "parse_multi", lambda text: {
        "entries": [dict(item), dict(item)], "stated_total": None,
    })
    entries = processor.process_text_multi("…").entries
    assert not entries[0].warnings
    assert "是否重複" in entries[1].warnings[0]


def test_format_confirmation_shows_period_and_warnings(processor):
    entry = AccountEntry(
        title="8月勞保", amount=11122, category="保險稅務", date="2026-10-04",
        period="2026-08", date_defaulted=True, parse_engine="llm", parse_confidence=0.95,
        warnings=["測試提醒"],
    )
    message = processor.format_confirmation(entry)
    assert "歸屬月份：2026-08" in message
    assert "以今天計" in message
    assert "⚠️ 測試提醒" in message
    assert "付款方式" in message and "對象" in message


@pytest.mark.parametrize("text, expected", [
    ("/支 飲料（特價） 75", "飲料（特價）"),
    ("/支 教室罰款分期第7期 5555", "教室罰款分期第7期"),
    ("/收 Kurt Workshop $15,300", "Kurt Workshop"),
])
def test_command_title_drops_amount_only(rule_processor, text, expected):
    assert rule_processor.process_text(text).title == expected


def test_period_keeps_explicit_year(rule_processor):
    """補記去年的帳：年份不能被換成今年，也不能被當成金額。"""
    entry = rule_processor.process_text("/支 2025年8月勞保 11122")
    assert entry.period == "2025-08"
    assert entry.amount == 11122
    assert entry.title == "2025年8月勞保"
