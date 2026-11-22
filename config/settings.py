"""補習班記帳系統 — 組態設定。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


class ConfigValidationError(Exception):
    """Raised when required environment configuration is missing or invalid."""

def normalize_notion_id(raw: str) -> str:
    """從字串中萃取出單一合法 UUID，避免因為誤貼多餘字綴導致 API 回傳 404/400。"""
    if not raw:
        return ""
    # 尋找標準的 36 字元 UUID 格式 (8-4-4-4-12)
    match = re.search(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', raw)
    return match.group(0) if match else ""


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigValidationError(f"{name} must be an integer, got: {raw}") from exc


@dataclass(frozen=True)
class LineConfig:
    """LINE Messaging API 設定。"""

    channel_access_token: str = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    channel_secret: str = os.getenv("LINE_CHANNEL_SECRET", "")


@dataclass(frozen=True)
class NotionConfig:
    """Notion API 設定。"""

    api_key: str = os.getenv("NOTION_API_KEY", "")
    database_id: str = normalize_notion_id(os.getenv("NOTION_DATABASE_ID", ""))
    api_version: str = os.getenv("NOTION_API_VERSION", "2022-06-28")
    # 「歸屬月份」欄位名稱。資料庫有這個欄位才會寫入，沒有則略過。
    period_property: str = os.getenv("NOTION_PERIOD_PROPERTY", "歸屬月份")


@dataclass(frozen=True)
class LLMConfig:
    """Google Gemini API 設定（含 Vision OCR）。"""

    api_key: str = os.getenv("GEMINI_API_KEY", "")
    model: str = os.getenv("LLM_MODEL", "models/gemini-2.5-flash")
    vision_model: str = os.getenv("VISION_MODEL", "models/gemini-2.5-flash")


@dataclass(frozen=True)
class AppConfig:
    """應用程式設定。"""

    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = _get_int("APP_PORT", 5000)
    debug: bool = _get_bool("APP_DEBUG", False)
    school_name: str = os.getenv("SCHOOL_NAME", "我的補習班")
    # 記帳日期使用的時區（相對 UTC 的小時數）。台灣為 +8，無日光節約時間。
    utc_offset_hours: int = _get_int("APP_UTC_OFFSET_HOURS", 8)


line_config = LineConfig()
notion_config = NotionConfig()
llm_config = LLMConfig()
app_config = AppConfig()


def local_now() -> datetime:
    """回傳記帳時區（預設台灣 UTC+8）的現在時間。

    伺服器（Railway）以 UTC 執行，直接用 datetime.now() 會讓台灣時間
    00:00–08:00 之間記的帳落到前一天。
    """
    return datetime.now(timezone(timedelta(hours=app_config.utc_offset_hours)))


def local_today() -> str:
    """回傳記帳時區的今天日期（YYYY-MM-DD）。"""
    return local_now().strftime("%Y-%m-%d")


def validate_config() -> bool:
    """驗證必要環境變數，建議在應用啟動時先呼叫。

    Raises:
        ConfigValidationError: 若有任何必要設定缺漏或格式錯誤。

    Returns:
        True 代表驗證通過。
    """
    missing_fields: list[str] = []

    required_values = {
        "LINE_CHANNEL_ACCESS_TOKEN": line_config.channel_access_token,
        "LINE_CHANNEL_SECRET": line_config.channel_secret,
        "NOTION_API_KEY": notion_config.api_key,
        "NOTION_DATABASE_ID": notion_config.database_id,
        "GEMINI_API_KEY": llm_config.api_key,
    }

    for name, value in required_values.items():
        if not value or not value.strip():
            missing_fields.append(name)

    errors: list[str] = []
    if missing_fields:
        errors.append("Missing required environment variables: " + ", ".join(missing_fields))

    if not (1 <= app_config.port <= 65535):
        errors.append(f"APP_PORT must be between 1 and 65535, got: {app_config.port}")

    if not (-12 <= app_config.utc_offset_hours <= 14):
        errors.append(
            f"APP_UTC_OFFSET_HOURS must be between -12 and 14, got: {app_config.utc_offset_hours}"
        )

    if llm_config.api_key and not llm_config.model:
        errors.append("LLM_MODEL cannot be empty when GEMINI_API_KEY is set")

    if llm_config.api_key and not llm_config.vision_model:
        errors.append("VISION_MODEL cannot be empty when GEMINI_API_KEY is set")

    if errors:
        raise ConfigValidationError("\n".join(errors))

    return True
