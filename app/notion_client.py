"""Notion API 記帳資料庫客戶端。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config.settings import local_today, notion_config

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
SCHEMA_CACHE_SECONDS = 600
SCHEMA_RETRY_SECONDS = 60
# 「刪除上一筆」只能撤銷這段時間內由本人記錄的資料
UNDO_WINDOW_HOURS = 24


class NotionClientError(Exception):
    pass


class NotionAccountClient:
    """補習班記帳 Notion 資料庫客戶端。"""

    def __init__(self) -> None:
        self.api_key = notion_config.api_key
        self.database_id = notion_config.database_id
        self.period_property = notion_config.period_property
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Notion-Version": notion_config.api_version,
        }
        self._schema: Optional[dict] = None
        self._schema_expires_at = 0.0

    # ── 寫入 ──────────────────────────────────────────────

    def create_account_entry(
        self,
        title: str,
        amount: float,
        entry_type: str = "支出",
        category: str = "其他支出",
        date: Optional[str] = None,
        vendor: str = "",
        payment_method: str = "未指定",
        description: str = "",
        invoice_number: str = "",
        tax_amount: float = 0,
        source_user: str = "",
        period: Optional[str] = None,
    ) -> dict:
        """建立一筆記帳紀錄並寫入 Notion 資料庫。

        Args:
            date: 實際收付日 YYYY-MM-DD；未提供時以今天（記帳時區）計。
            period: 歸屬月份 YYYY-MM。資料庫有「歸屬月份」欄位時寫入該欄；
                沒有該欄位且歸屬月份與日期不同月時，改寫在備註開頭。
        """
        today_date = local_today()
        formatted_date = self._format_date(date) or today_date

        description = str(description or "")
        period_value = self._period_property_value(period)
        if period and period_value is None and period != formatted_date[:7]:
            description = f"〔歸屬月份 {period}〕{description}"

        properties: dict = {
            "摘要": {
                "title": [{"text": {"content": title}}]
            },
            "金額": {"number": float(amount)},
            "收支類型": {"select": {"name": entry_type}},
            "分類": {"select": {"name": category}},
            "付款方式": {"select": {"name": payment_method}},
            "備註": {
                "rich_text": [{"text": {"content": description[:2000]}}]
                if description else []
            },
            "日期": {"date": {"start": formatted_date}},
            "記錄時間": {"date": {"start": today_date}},
        }

        if period_value is not None:
            properties[self.period_property] = period_value

        if vendor:
            properties["對象/廠商"] = {
                "rich_text": [{"text": {"content": str(vendor)}}]
            }

        if invoice_number:
            properties["發票號碼"] = {
                "rich_text": [{"text": {"content": str(invoice_number)}}]
            }

        if tax_amount is not None:
            properties["稅額"] = {"number": float(tax_amount)}

        if source_user:
            properties["記錄人"] = {
                "rich_text": [{"text": {"content": str(source_user)}}]
            }

        return self._create_page(properties)

    @staticmethod
    def _format_date(date: Optional[str]) -> Optional[str]:
        if not date:
            return None
        try:
            return datetime.fromisoformat(str(date).replace("/", "-")).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    def _period_property_value(self, period: Optional[str]) -> Optional[dict]:
        """依資料庫中「歸屬月份」欄位的型別組出寫入值；欄位不存在回傳 None。"""
        if not period or not self.period_property:
            return None
        prop_type = self._cached_schema().get(self.period_property, {}).get("type")
        if prop_type == "select":
            return {"select": {"name": period}}
        if prop_type == "rich_text":
            return {"rich_text": [{"text": {"content": period}}]}
        if prop_type == "date":
            return {"date": {"start": f"{period}-01"}}
        return None

    def has_period_property(self) -> bool:
        """資料庫是否已建立可寫入的「歸屬月份」欄位。"""
        return self._period_property_value("2000-01") is not None

    def _cached_schema(self) -> dict:
        """讀取資料庫欄位並快取；讀不到時當作沒有額外欄位，不影響寫入。"""
        now = time.monotonic()
        if self._schema is not None and now < self._schema_expires_at:
            return self._schema
        try:
            self._schema = self.get_database_schema()
            self._schema_expires_at = now + SCHEMA_CACHE_SECONDS
        except NotionClientError as exc:
            logger.warning("Could not read Notion schema: %s", exc)
            self._schema = {}
            self._schema_expires_at = now + SCHEMA_RETRY_SECONDS
        return self._schema

    def _create_page(self, properties: dict) -> dict:
        """向 Notion API 提交一頁（一筆記帳記錄）。"""
        self._require_database_id()
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        return self._request("POST", f"{NOTION_API_BASE}/pages", payload)

    # ── 查詢 / 撤銷 ───────────────────────────────────────

    def find_possible_duplicates(
        self, date: str, amount: float, entry_type: str
    ) -> list[dict]:
        """找出同一天、同金額、同收支類型的既有紀錄。

        Returns:
            [{"id": page_id, "title": 摘要, "recorder": 記錄人,
              "amount": 金額, "created_time": Notion 建立時間}, ...]
        """
        payload = {
            "filter": {
                "and": [
                    {"property": "日期", "date": {"equals": date}},
                    {"property": "金額", "number": {"equals": float(amount)}},
                    {"property": "收支類型", "select": {"equals": entry_type}},
                ]
            },
            "page_size": 20,
        }
        return [self._summarize_page(page) for page in self._query(payload)]

    def find_last_entry_by(self, source_user: str) -> Optional[dict]:
        """找出該記錄人最近一筆（限 UNDO_WINDOW_HOURS 內建立）的紀錄。"""
        since = datetime.now(timezone.utc) - timedelta(hours=UNDO_WINDOW_HOURS)
        payload = {
            "filter": {
                "and": [
                    {"property": "記錄人", "rich_text": {"equals": source_user}},
                    {
                        "timestamp": "created_time",
                        "created_time": {"on_or_after": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
                    },
                ]
            },
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 1,
        }
        pages = self._query(payload)
        return self._summarize_page(pages[0]) if pages else None

    def archive_entry(self, page_id: str) -> dict:
        """把一筆紀錄移到 Notion 垃圾桶（30 天內可在 Notion 還原）。"""
        return self._request(
            "PATCH", f"{NOTION_API_BASE}/pages/{page_id}", {"archived": True}
        )

    def _query(self, payload: dict) -> list[dict]:
        self._require_database_id()
        url = f"{NOTION_API_BASE}/databases/{self.database_id}/query"
        return self._request("POST", url, payload).get("results", [])

    @staticmethod
    def _summarize_page(page: dict) -> dict:
        props = page.get("properties", {})

        def plain(name: str, key: str) -> str:
            return "".join(
                part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in props.get(name, {}).get(key, []) or []
            )

        return {
            "id": page.get("id", ""),
            "title": plain("摘要", "title"),
            "recorder": plain("記錄人", "rich_text"),
            "amount": props.get("金額", {}).get("number"),
            "created_time": page.get("created_time", ""),
        }

    # ── HTTP ──────────────────────────────────────────────

    def _require_database_id(self) -> None:
        if not self.database_id:
            raise NotionClientError("NOTION_DATABASE_ID is empty")
        if len(self.database_id) != 36:
            raise NotionClientError(f"Invalid NOTION_DATABASE_ID: {self.database_id}")

    def _request(self, method: str, url: str, payload: dict) -> dict:
        try:
            response = requests.request(
                method, url, headers=self.headers, json=payload, timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else str(exc)
            status_code = exc.response.status_code if exc.response is not None else "Unknown"
            logger.error(
                "Notion API Error. %s %s Status: %s, Response: %s",
                method, url, status_code, error_body,
            )
            raise NotionClientError(
                f"Failed (Status {status_code}): {error_body}"
            ) from exc
        except requests.RequestException as exc:
            raise NotionClientError(f"Request failed: {exc}") from exc

    def get_database_schema(self) -> dict:
        """讀取 Notion 資料庫 Schema。"""
        url = f"{NOTION_API_BASE}/databases/{self.database_id}"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json().get("properties", {})
        except requests.RequestException as exc:
            raise NotionClientError(f"Schema retrieval failed: {exc}") from exc
