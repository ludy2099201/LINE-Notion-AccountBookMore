"""Bookkeeper 流程測試：查重、撤銷、修正訊息、測試模式、多筆寫入。"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.bookkeeper import CORRECTION_REPLY, PARSE_FAILURE_REPLY, Bookkeeper
from app.data_processor import AccountEntry, DataProcessor
from app.notion_client import NotionClientError


def _llm_result(entries, stated_total=None):
    base = {
        "type": "支出", "category": "其他支出", "date": None, "period": None,
        "vendor": "", "payment_method": "未指定", "description": "",
        "invoice_number": "", "tax_amount": 0, "components": [],
    }
    return {
        "entries": [{**base, **entry} for entry in entries],
        "stated_total": stated_total,
        "truncated": False,
    }


@pytest.fixture
def notion():
    client = MagicMock()
    client.find_possible_duplicates.return_value = []
    client.create_account_entry.return_value = {"id": "page-1"}
    return client


@pytest.fixture
def processor(monkeypatch):
    proc = DataProcessor()
    monkeypatch.setattr(proc.llm_parser, "is_available", lambda: False)
    return proc


@pytest.fixture
def bookkeeper(processor, notion):
    return Bookkeeper(processor, notion)


def _use_llm(processor, monkeypatch, result):
    monkeypatch.setattr(processor.llm_parser, "is_available", lambda: True)
    monkeypatch.setattr(processor.llm_parser, "parse_multi", lambda text: result)


# ── 一般寫入 ──────────────────────────────────────────

def test_single_entry_is_written(bookkeeper, notion):
    reply = bookkeeper.handle_text("/支 講義 4500", "Mia")

    notion.create_account_entry.assert_called_once()
    kwargs = notion.create_account_entry.call_args.kwargs
    assert kwargs["title"] == "講義"
    assert kwargs["amount"] == 4500
    assert kwargs["source_user"] == "Mia"
    assert kwargs["date"] and kwargs["period"] == kwargs["date"][:7]
    assert "已記錄到 Notion" in reply
    assert "以今天計" in reply           # 沒寫日期要明說
    assert "付款方式" in reply           # 提醒沒填的欄位


def test_no_amount_is_not_written(bookkeeper, notion):
    reply = bookkeeper.handle_text("今天天氣很好", "Mia")
    assert reply == PARSE_FAILURE_REPLY
    notion.create_account_entry.assert_not_called()


def test_notion_failure_reports_nothing_written(bookkeeper, notion):
    notion.create_account_entry.side_effect = NotionClientError("Failed (Status 400)")
    reply = bookkeeper.handle_text("/支 講義 4500", "Mia")
    assert "發生錯誤" in reply and "沒有任何資料被寫入" in reply


# ── 修正訊息 / 測試模式 ───────────────────────────────

def test_correction_message_is_not_recorded(bookkeeper, notion):
    """帳本裡「請修正 這個是收入…」曾被當成新的一筆寫入。"""
    reply = bookkeeper.handle_text("請修正 這個是收入：- Liam 七月Workshop $15,300", "Mia")
    assert reply == CORRECTION_REPLY
    notion.create_account_entry.assert_not_called()


def test_dry_run_previews_without_writing(bookkeeper, notion):
    reply = bookkeeper.handle_text("測試 買講義 500 元", "Mia")
    assert "沒有」寫入" in reply and "$500" in reply
    notion.create_account_entry.assert_not_called()
    notion.find_possible_duplicates.assert_not_called()


def test_dry_run_without_content_explains_usage(bookkeeper, notion):
    assert "測試模式" in bookkeeper.handle_text("測試", "Mia")
    notion.create_account_entry.assert_not_called()


def test_legit_entry_containing_test_word_is_recorded(bookkeeper, notion):
    bookkeeper.handle_text("測試卷印刷 300 元", "Mia")
    notion.create_account_entry.assert_called_once()


# ── 重複偵測 ──────────────────────────────────────────

def test_duplicate_is_written_with_warning(bookkeeper, notion):
    """同日同金額、標題相近：照寫但提醒，並告知如何撤銷。"""
    notion.find_possible_duplicates.return_value = [
        {"id": "p0", "title": "飲料", "recorder": "Mia", "amount": 75}
    ]
    reply = bookkeeper.handle_text("/支 飲料（特價） 75", "Mia")
    notion.create_account_entry.assert_called_once()
    assert "可能重複" in reply and "刪除上一筆" in reply


def test_same_amount_different_student_is_not_flagged(bookkeeper, notion):
    notion.find_possible_duplicates.return_value = [
        {"id": "p0", "title": "Kurt 暑期常態課程", "recorder": "Mia", "amount": 6500}
    ]
    reply = bookkeeper.handle_text("/收 Matt 暑期常態課程 6500", "Mia")
    assert "可能重複" not in reply


def test_duplicate_check_failure_does_not_block_recording(bookkeeper, notion):
    notion.find_possible_duplicates.side_effect = NotionClientError("403")
    reply = bookkeeper.handle_text("/支 講義 4500", "Mia")
    notion.create_account_entry.assert_called_once()
    assert "已記錄到 Notion" in reply


def test_redelivery_of_recorded_message_is_skipped(bookkeeper, notion):
    notion.find_possible_duplicates.return_value = [
        {"id": "p0", "title": "講義", "recorder": "Mia", "amount": 4500}
    ]
    reply = bookkeeper.handle_text("/支 講義 4500", "Mia", is_redelivery=True)
    notion.create_account_entry.assert_not_called()
    assert "已經記錄過" in reply


SENT_AT = datetime(2026, 11, 22, 7, 5, 40, tzinfo=timezone.utc)


def test_redelivery_skips_entry_written_by_first_delivery(bookkeeper, notion):
    notion.find_possible_duplicates.return_value = [{
        "id": "p0", "title": "飲料", "recorder": "Mia", "amount": 75,
        "created_time": "2026-11-22T07:05:00.000Z",     # Notion 只精確到分鐘
    }]
    reply = bookkeeper.handle_text("/支 飲料 75", "Mia", is_redelivery=True, event_time=SENT_AT)
    notion.create_account_entry.assert_not_called()
    assert "已經記錄過" in reply


def test_redelivery_does_not_drop_legitimate_repeat(bookkeeper, notion):
    """早上已記一筆「飲料 75」，下午又買一次；這則訊息第一次送達時沒寫成功。

    帳本裡那筆是訊息送出前就存在的，不是這則訊息寫的，所以要照常寫入。
    """
    notion.find_possible_duplicates.return_value = [{
        "id": "p0", "title": "飲料", "recorder": "Mia", "amount": 75,
        "created_time": "2026-11-22T01:30:00.000Z",
    }]
    reply = bookkeeper.handle_text("/支 飲料 75", "Mia", is_redelivery=True, event_time=SENT_AT)
    notion.create_account_entry.assert_called_once()
    assert "可能重複" in reply and "已經記錄過" not in reply


def test_redelivery_of_unrecorded_message_is_written(bookkeeper, notion):
    """第一次送達時沒寫成功（帳本查不到），重送時要補寫。"""
    bookkeeper.handle_text("/支 講義 4500", "Mia", is_redelivery=True)
    notion.create_account_entry.assert_called_once()


# ── 多筆 ──────────────────────────────────────────────

FAMILY_MESSAGE = """- Liam 七月Workshop
⭐️早鳥優惠價 $15,300

- Lyanna 七月Workshop
⭐️早鳥優惠價 $15,300

- Liam&Lyanna 暑期常態課程 $6,500*2=13,000

總共需繳納：$43,600"""


def test_multi_entry_message_writes_each_entry(bookkeeper, processor, notion, monkeypatch):
    """帳本裡這類訊息只記到第一筆，其餘要重傳。"""
    _use_llm(processor, monkeypatch, _llm_result([
        {"title": "Liam 七月Workshop", "amount": 15300, "type": "收入", "category": "學費收入", "vendor": "Liam"},
        {"title": "Lyanna 七月Workshop", "amount": 15300, "type": "收入", "category": "報名費", "vendor": "Lyanna"},
        {"title": "Liam&Lyanna 暑期常態課程", "amount": 13000, "type": "收入", "category": "學費收入"},
    ], stated_total=43600))

    reply = bookkeeper.handle_text(FAMILY_MESSAGE, "Mia")

    assert notion.create_account_entry.call_count == 3
    categories = [c.kwargs["category"] for c in notion.create_account_entry.call_args_list]
    assert categories == ["學費收入"] * 3          # Workshop 不再被記成報名費
    assert "已記錄 3 筆" in reply
    assert "收入合計：$43,600" in reply
    assert "可能有漏記" not in reply


def test_multi_entry_total_mismatch_warns(bookkeeper, processor, notion, monkeypatch):
    _use_llm(processor, monkeypatch, _llm_result([
        {"title": "Liam 七月Workshop", "amount": 15300, "type": "收入", "category": "學費收入"},
        {"title": "Liam&Lyanna 暑期常態課程", "amount": 13000, "type": "收入", "category": "學費收入"},
    ], stated_total=43600))
    reply = bookkeeper.handle_text(FAMILY_MESSAGE, "Mia")
    assert "差 15,300 元" in reply


def test_multi_entry_partial_failure_is_reported(bookkeeper, processor, notion, monkeypatch):
    _use_llm(processor, monkeypatch, _llm_result([
        {"title": "房租", "amount": 40369, "category": "房租水電"},
        {"title": "11月管理費", "amount": 10698, "category": "其他支出"},
    ]))
    notion.create_account_entry.side_effect = [{"id": "p1"}, NotionClientError("Failed (Status 502)")]
    reply = bookkeeper.handle_text("房租 40369\n11月管理費 10698", "Mia")
    assert "房租 $40,369" in reply
    assert "寫入失敗" in reply and "11月管理費" in reply


def test_entries_without_amount_are_listed_not_written(bookkeeper, processor, notion, monkeypatch):
    _use_llm(processor, monkeypatch, _llm_result([
        {"title": "房租", "amount": 40369, "category": "房租水電"},
        {"title": "電費", "amount": 0, "category": "房租水電"},
    ]))
    reply = bookkeeper.handle_text("房租 40369\n電費還沒收到帳單", "Mia")
    assert notion.create_account_entry.call_count == 1
    assert "沒有金額，沒有記錄：電費" in reply


# ── 薪資檢核 ──────────────────────────────────────────

def test_salary_breakdown_mismatch_and_reimbursement(bookkeeper, processor, notion, monkeypatch):
    """Celia 3 月薪資：明細加總與金額差 10 元，且內含已另外記過的代墊款。"""
    _use_llm(processor, monkeypatch, _llm_result([{
        "title": "Celia 3月薪資", "amount": 45214, "category": "人事費用", "vendor": "Celia",
        "period": "2026-03",
        "components": [
            {"label": "底薪", "amount": 42750},
            {"label": "勞保自付額", "amount": -738},
            {"label": "健保自付額", "amount": -458},
            {"label": "其他扣款", "amount": -199},
            {"label": "代墊款項", "amount": 3869},
        ],
    }]))
    reply = bookkeeper.handle_text("Celia 3月薪資Total: 45214\n💰 42750\n…", "Mia")

    kwargs = notion.create_account_entry.call_args.kwargs
    assert kwargs["period"] == "2026-03"
    assert kwargs["vendor"] == "Celia"
    assert "差 10 元" in reply
    assert "代墊款 3,869" in reply


# ── 撤銷 ──────────────────────────────────────────────

def test_undo_archives_latest_entry(bookkeeper, notion):
    notion.find_last_entry_by.return_value = {"id": "p9", "title": "飲料", "recorder": "Mia", "amount": 75}
    reply = bookkeeper.handle_text("刪除上一筆", "Mia")
    notion.find_last_entry_by.assert_called_once_with("Mia")
    notion.archive_entry.assert_called_once_with("p9")
    notion.create_account_entry.assert_not_called()
    assert "飲料 $75" in reply


def test_redelivered_undo_does_not_delete_again(bookkeeper, notion):
    """第一次送達已刪掉一筆；重送若再執行會把前一筆也刪掉。"""
    reply = bookkeeper.handle_text("刪除上一筆", "Mia", is_redelivery=True)
    notion.find_last_entry_by.assert_not_called()
    notion.archive_entry.assert_not_called()
    assert "沒有執行" in reply


def test_undo_without_recent_entry_deletes_nothing(bookkeeper, notion):
    notion.find_last_entry_by.return_value = None
    reply = bookkeeper.handle_text("/undo", "Mia")
    notion.archive_entry.assert_not_called()
    assert "沒有刪除任何東西" in reply


def test_undo_requires_known_user(bookkeeper, notion):
    reply = bookkeeper.handle_text("刪除上一筆", "Unknown")
    notion.find_last_entry_by.assert_not_called()
    notion.archive_entry.assert_not_called()
    assert "沒有刪除任何資料" in reply


def test_undo_failure_is_reported(bookkeeper, notion):
    notion.find_last_entry_by.side_effect = NotionClientError("Failed (Status 403)")
    reply = bookkeeper.handle_text("刪除上一筆", "Mia")
    assert "撤銷失敗" in reply


# ── OCR ───────────────────────────────────────────────

def test_ocr_entry_is_written(bookkeeper, notion):
    entry = AccountEntry(
        title="珍煮丹", amount=417, category="餐飲點心", date="2026-03-14",
        period="2026-03", vendor="珍煮丹", parse_engine="ocr", parse_confidence=0.9,
    )
    reply = bookkeeper.handle_ocr_entry(entry, "Mia")
    notion.create_account_entry.assert_called_once()
    assert "已記錄到 Notion" in reply
