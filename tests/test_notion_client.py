"""Notion 客戶端測試：日期 / 歸屬月份寫入、查重與撤銷查詢。"""

from unittest.mock import MagicMock

import pytest
import requests

from app.notion_client import NotionAccountClient, NotionClientError

DB_ID = "2856d80f-b494-4965-9e1e-9c7700cb0edf"


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    cfg = MagicMock()
    cfg.api_key = "secret"
    cfg.database_id = DB_ID
    cfg.api_version = "2022-06-28"
    cfg.period_property = "歸屬月份"
    monkeypatch.setattr("app.notion_client.notion_config", cfg)
    monkeypatch.setattr("app.notion_client.local_today", lambda: "2026-10-04")
    return NotionAccountClient()


def _capture(monkeypatch, schema=None, results=None):
    """攔截所有 Notion 請求，回傳 (method, url, payload) 紀錄。"""
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append((method, url, json))
        return DummyResponse({"id": "page-1", "results": results or []})

    monkeypatch.setattr("app.notion_client.requests.request", fake_request)
    monkeypatch.setattr(
        "app.notion_client.requests.get",
        lambda *a, **k: DummyResponse({"properties": schema or {}}),
    )
    return calls


def test_date_defaults_to_local_today(client, monkeypatch):
    calls = _capture(monkeypatch)
    client.create_account_entry(title="講義", amount=500)
    props = calls[0][2]["properties"]
    assert props["日期"]["date"]["start"] == "2026-10-04"
    assert props["記錄時間"]["date"]["start"] == "2026-10-04"


def test_explicit_date_is_kept_separate_from_recorded_time(client, monkeypatch):
    calls = _capture(monkeypatch)
    client.create_account_entry(title="8月健保", amount=5943, date="2026/08/04")
    props = calls[0][2]["properties"]
    assert props["日期"]["date"]["start"] == "2026-08-04"
    assert props["記錄時間"]["date"]["start"] == "2026-10-04"


@pytest.mark.parametrize("prop_type, expected", [
    ("select", {"select": {"name": "2026-08"}}),
    ("rich_text", {"rich_text": [{"text": {"content": "2026-08"}}]}),
    ("date", {"date": {"start": "2026-08-01"}}),
])
def test_period_written_to_property_when_it_exists(client, monkeypatch, prop_type, expected):
    calls = _capture(monkeypatch, schema={"歸屬月份": {"type": prop_type}})
    client.create_account_entry(title="8月勞保", amount=11122, period="2026-08")
    props = calls[0][2]["properties"]
    assert props["歸屬月份"] == expected
    assert props["備註"]["rich_text"] == []
    assert client.has_period_property()


def test_period_falls_back_to_note_when_property_missing(client, monkeypatch):
    """資料庫還沒有「歸屬月份」欄位時不能送出該欄位（Notion 會回 400）。"""
    calls = _capture(monkeypatch, schema={})
    client.create_account_entry(title="8月勞保", amount=11122, period="2026-08", description="補記")
    props = calls[0][2]["properties"]
    assert "歸屬月份" not in props
    assert props["備註"]["rich_text"][0]["text"]["content"] == "〔歸屬月份 2026-08〕補記"
    assert not client.has_period_property()


def test_period_same_month_as_date_adds_no_note(client, monkeypatch):
    calls = _capture(monkeypatch, schema={})
    client.create_account_entry(title="房租", amount=40369, period="2026-10")
    assert calls[0][2]["properties"]["備註"]["rich_text"] == []


def test_schema_failure_does_not_block_writing(client, monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        "app.notion_client.requests.get",
        lambda *a, **k: DummyResponse({}, status_code=403),
    )
    client.create_account_entry(title="8月勞保", amount=11122, period="2026-08")
    assert len(calls) == 1 and "歸屬月份" not in calls[0][2]["properties"]


def test_schema_is_cached(client, monkeypatch):
    _capture(monkeypatch)
    fetches = {"n": 0}

    def fake_get(*args, **kwargs):
        fetches["n"] += 1
        return DummyResponse({"properties": {"歸屬月份": {"type": "select"}}})

    monkeypatch.setattr("app.notion_client.requests.get", fake_get)
    for _ in range(3):
        client.create_account_entry(title="x", amount=1, period="2026-08")
    assert fetches["n"] == 1


def test_find_possible_duplicates_filters_by_date_amount_type(client, monkeypatch):
    page = {
        "id": "p0",
        "created_time": "2026-04-11T03:05:00.000Z",
        "properties": {
            "摘要": {"title": [{"plain_text": "Jennie Workshop 早鳥優惠價"}]},
            "記錄人": {"rich_text": [{"plain_text": "Mia"}]},
            "金額": {"number": 15300},
        },
    }
    calls = _capture(monkeypatch, results=[page])
    found = client.find_possible_duplicates("2026-04-11", 15300, "收入")

    method, url, payload = calls[0]
    assert (method, url) == ("POST", f"https://api.notion.com/v1/databases/{DB_ID}/query")
    assert {"property": "日期", "date": {"equals": "2026-04-11"}} in payload["filter"]["and"]
    assert {"property": "金額", "number": {"equals": 15300.0}} in payload["filter"]["and"]
    assert {"property": "收支類型", "select": {"equals": "收入"}} in payload["filter"]["and"]
    assert found == [{
        "id": "p0", "title": "Jennie Workshop 早鳥優惠價", "recorder": "Mia",
        "amount": 15300, "created_time": "2026-04-11T03:05:00.000Z",
    }]


def test_find_last_entry_by_limits_to_recorder_and_recent(client, monkeypatch):
    calls = _capture(monkeypatch, results=[])
    assert client.find_last_entry_by("Mia") is None
    payload = calls[0][2]
    assert {"property": "記錄人", "rich_text": {"equals": "Mia"}} in payload["filter"]["and"]
    assert any(f.get("timestamp") == "created_time" for f in payload["filter"]["and"])
    assert payload["sorts"] == [{"timestamp": "created_time", "direction": "descending"}]
    assert payload["page_size"] == 1


def test_archive_entry(client, monkeypatch):
    calls = _capture(monkeypatch)
    client.archive_entry("p9")
    assert calls[0] == ("PATCH", "https://api.notion.com/v1/pages/p9", {"archived": True})


def test_http_error_raises_client_error(client, monkeypatch):
    monkeypatch.setattr(
        "app.notion_client.requests.request",
        lambda *a, **k: DummyResponse({"message": "bad"}, status_code=400),
    )
    monkeypatch.setattr("app.notion_client.requests.get", lambda *a, **k: DummyResponse({"properties": {}}))
    with pytest.raises(NotionClientError, match="Status 400"):
        client.create_account_entry(title="x", amount=1)


def test_invalid_database_id_rejected(client):
    client.database_id = "not-a-uuid"
    with pytest.raises(NotionClientError):
        client.create_account_entry(title="x", amount=1)
