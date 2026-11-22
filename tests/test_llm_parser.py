"""
LLM 解析引擎單元測試（Task #10 — 優化 ⑩）。

使用 pytest + monkeypatch，涵蓋以下情境：
- 初始化（有 / 無 API key）
- 成功 parse 流程
- _validate_result() 分類 fallback / 無標題 / 無效金額
- API 失敗回傳 None（含重試次數驗證）
- code fence 自動清理

Note: config/settings.py 使用 frozen=True dataclass，
以 _mock_llm_config() 整個替換 llm_config 物件（MagicMock）。
"""

import json
from unittest.mock import MagicMock

import pytest
import requests

from app.llm_parser import LLMParser, MAX_RETRIES, VALID_CATEGORIES


# ── 輔助 stub ─────────────────────────────────────────

class DummyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _mock_llm_config(api_key: str, model: str = "models/test"):
    """建立 frozen=True 相容的 llm_config mock。"""
    cfg = MagicMock()
    cfg.api_key = api_key
    cfg.model = model
    cfg.vision_model = model
    return cfg


# ── 初始化測試 ────────────────────────────────────────

def test_init_without_api_key(monkeypatch):
    """無 API key 時 is_available() 應回傳 False。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config(""))
    parser = LLMParser()
    assert parser.is_available() is False


def test_init_with_api_key(monkeypatch):
    """有 API key 時 is_available() 應回傳 True。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()
    assert parser.is_available() is True


# ── 成功路徑 ──────────────────────────────────────────

def test_parse_success(monkeypatch):
    """正常 parse 應回傳完整結構化 dict，欄位對齊驗證。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    parsed_data = {
        "title": "講義採購",
        "amount": 4500,
        "type": "支出",
        "category": "教材費",
        "date": "2026-04-05",
        "vendor": "書局",
        "payment_method": "現金",
        "description": "買了 30 本講義",
        "invoice_number": "AB12345678",
        "tax_amount": 0,
    }
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(parsed_data, ensure_ascii=False)}]}}
        ]
    }
    monkeypatch.setattr(
        "app.llm_parser.requests.post",
        lambda *args, **kwargs: DummyResponse(payload),
    )
    result = parser.parse("買了 30 本講義 $4500")

    assert result is not None
    assert result["title"] == "講義採購"
    assert result["category"] == "教材費"
    assert result["amount"] == 4500.0
    assert result["vendor"] == "書局"
    assert result["invoice_number"] == "AB12345678"


def test_parse_strips_code_fence(monkeypatch):
    """Gemini 回傳 JSON 含 code fence 時應自動清除後正確解析。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    fenced = (
        "```json\n"
        '{"title": "電費", "amount": 3200, "type": "支出", "category": "房租水電"}\n'
        "```"
    )
    payload = {"candidates": [{"content": {"parts": [{"text": fenced}]}}]}
    monkeypatch.setattr(
        "app.llm_parser.requests.post",
        lambda *args, **kwargs: DummyResponse(payload),
    )
    result = parser.parse("繳電費 3200")

    assert result is not None
    assert result["title"] == "電費"
    assert result["category"] == "房租水電"


# ── _validate_result 正規化 ───────────────────────────

def test_validate_result_invalid_category_fallback(monkeypatch):
    """收入類型使用不存在分類時，應 fallback 為「其他收入」。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    result = parser._validate_result({
        "title": "未知分類收入",
        "amount": 12000,
        "type": "收入",
        "category": "神秘分類",
    })
    assert result is not None
    assert result["category"] == "其他收入"


def test_validate_result_expense_category_fallback(monkeypatch):
    """支出類型使用不存在分類時，應 fallback 為「其他支出」。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    result = parser._validate_result({
        "title": "奇怪支出",
        "amount": 500,
        "type": "支出",
        "category": "不存在",
    })
    assert result["category"] == "其他支出"


def test_validate_result_missing_title_returns_none(monkeypatch):
    """缺少 title 時應回傳 None。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    assert parser._validate_result({"amount": 100}) is None


def test_validate_result_invalid_amount_falls_back_to_zero(monkeypatch):
    """無效金額字串應正規化為 0.0。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    result = parser._validate_result({
        "title": "測試",
        "amount": "abc",
        "type": "支出",
        "category": "教材費",
    })
    assert result is not None
    assert result["amount"] == 0.0


def test_validate_result_negative_amount_becomes_positive(monkeypatch):
    """負數金額應取絕對值。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    result = parser._validate_result({
        "title": "退款",
        "amount": -500,
        "type": "收入",
        "category": "其他收入",
    })
    assert result["amount"] == 500.0


def test_validate_result_invalid_type_defaults_to_expense(monkeypatch):
    """無效 type 欄位應 fallback 為「支出」。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    result = parser._validate_result({
        "title": "測試",
        "amount": 100,
        "type": "未知",
        "category": "教材費",
    })
    assert result["type"] == "支出"


# ── 失敗路徑 ──────────────────────────────────────────

def test_parse_api_failure_returns_none(monkeypatch):
    """
    API 呼叫拋出 requests.RequestException 時應重試後回傳 None。

    Note: _post_with_retry 精確捕捉 requests.RequestException（Task #5 規格），
    因此 monkeypatch 拋出 RequestException 而非裸 Exception。
    """
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    call_count = {"n": 0}

    def raise_request_error(*args, **kwargs):
        call_count["n"] += 1
        raise requests.RequestException("network error")

    monkeypatch.setattr("app.llm_parser.requests.post", raise_request_error)
    result = parser.parse("買了講義 4500")

    assert result is None
    assert call_count["n"] == MAX_RETRIES + 1


def test_parse_disabled_returns_none(monkeypatch):
    """無 API key 時 parse() 應直接回傳 None，不發出 HTTP 請求。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config(""))
    parser = LLMParser()

    call_count = {"n": 0}

    def should_not_call(*args, **kwargs):
        call_count["n"] += 1

    monkeypatch.setattr("app.llm_parser.requests.post", should_not_call)
    result = parser.parse("買講義 1000")

    assert result is None
    assert call_count["n"] == 0


def test_parse_invalid_json_returns_none(monkeypatch):
    """Gemini 回傳無效 JSON 時應回傳 None。"""
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()

    payload = {"candidates": [{"content": {"parts": [{"text": "NOT JSON"}]}}]}
    monkeypatch.setattr(
        "app.llm_parser.requests.post",
        lambda *args, **kwargs: DummyResponse(payload),
    )
    result = parser.parse("測試")
    assert result is None


# ── 常數驗證 ──────────────────────────────────────────

def test_valid_categories_count():
    """VALID_CATEGORIES 應包含 14 種分類。"""
    assert len(VALID_CATEGORIES) == 14


def test_valid_categories_includes_all_types():
    """VALID_CATEGORIES 應同時包含收入與支出分類。"""
    income = {"學費收入", "教材收入", "報名費", "其他收入"}
    expense = {"教材費", "房租水電", "人事費用", "設備維護", "辦公用品",
               "餐飲點心", "交通費", "廣告行銷", "保險稅務", "其他支出"}
    assert income.issubset(VALID_CATEGORIES)
    assert expense.issubset(VALID_CATEGORIES)


# ── 多筆 / 歸屬月份 / 欄位正規化 ──────────────────────

def _parser_returning(monkeypatch, body):
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(body, ensure_ascii=False)}]}}]}
    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return DummyResponse(payload)

    monkeypatch.setattr("app.llm_parser.requests.post", fake_post)
    return LLMParser(), captured


def test_parse_multi_returns_every_entry(monkeypatch):
    parser, _ = _parser_returning(monkeypatch, {
        "entries": [
            {"title": "Liam 七月Workshop", "amount": 15300, "type": "收入", "category": "學費收入"},
            {"title": "Lyanna 七月Workshop", "amount": "15,300", "type": "收入", "category": "學費收入"},
            {"title": "", "amount": 1},                      # 沒有摘要 → 略過
        ],
        "stated_total": 30600,
    })
    result = parser.parse_multi("…")
    assert [e["title"] for e in result["entries"]] == ["Liam 七月Workshop", "Lyanna 七月Workshop"]
    assert result["entries"][1]["amount"] == 15300.0
    assert result["stated_total"] == 30600.0
    assert result["truncated"] is False


def test_parse_multi_accepts_bare_list_and_single_object(monkeypatch):
    parser, _ = _parser_returning(monkeypatch, [{"title": "房租", "amount": 40369}])
    assert len(parser.parse_multi("房租 40369")["entries"]) == 1

    parser, _ = _parser_returning(monkeypatch, {"title": "房租", "amount": 40369})
    assert parser.parse("房租 40369")["title"] == "房租"


def test_parse_multi_caps_entries(monkeypatch):
    from app.llm_parser import MAX_ENTRIES_PER_MESSAGE

    many = [{"title": f"項目{i}", "amount": 100} for i in range(MAX_ENTRIES_PER_MESSAGE + 3)]
    parser, _ = _parser_returning(monkeypatch, {"entries": many})
    result = parser.parse_multi("…")
    assert len(result["entries"]) == MAX_ENTRIES_PER_MESSAGE
    assert result["truncated"] is True


def test_request_allows_long_output(monkeypatch):
    """輸出上限太小會讓薪資明細這類長訊息的 JSON 被截斷，整則退回規則引擎。"""
    parser, captured = _parser_returning(monkeypatch, {"title": "x", "amount": 1})
    parser.parse_multi("x 1")
    assert captured["payload"]["generationConfig"]["maxOutputTokens"] >= 4096


def test_validate_result_normalizes_date_period_and_payment(monkeypatch):
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()
    result = parser._validate_result({
        "title": "8月勞保", "amount": 11122, "type": "支出", "category": "保險稅務",
        "date": "2026/10/4", "period": "2026-8", "payment_method": "匯款",
        "vendor": None, "components": [{"label": "a", "amount": "1,000"}, "bad"],
    })
    assert result["date"] == "2026-10-04"
    assert result["period"] == "2026-08"
    assert result["payment_method"] == "轉帳"
    assert result["vendor"] == ""
    assert result["components"] == [{"label": "a", "amount": 1000.0}]


@pytest.mark.parametrize("raw, expected", [
    ("未知", "未指定"),          # OCR 曾回傳「未知」，在 Notion 多出一個選項
    ("", "未指定"),
    (None, "未指定"),
    ("信用卡", "刷卡"),
    ("line pay", "LINE Pay"),
    ("現金", "現金"),
    ("銀行轉帳", "轉帳"),
])
def test_normalize_payment_method(raw, expected):
    from app.llm_parser import normalize_payment_method

    assert normalize_payment_method(raw) == expected


def test_validate_result_rejects_bad_date_and_period(monkeypatch):
    monkeypatch.setattr("app.llm_parser.llm_config", _mock_llm_config("test-key"))
    parser = LLMParser()
    result = parser._validate_result({
        "title": "x", "amount": 1, "date": "上週五", "period": "2026-13",
    })
    assert result["date"] is None and result["period"] is None
