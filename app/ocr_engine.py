"""OCR Engine — 使用 Google Gemini Vision API 辨識發票/收據。

支援：
- 台灣電子發票（統一發票）
- 手寫收據
- 超商/賣場發票
- 一般商業收據
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

import requests

from config.settings import llm_config

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 2
BASE_BACKOFF_SECONDS = 1

OCR_SYSTEM_PROMPT = """你是一個專業的發票/收據 OCR 辨識助手，專門為補習班記帳服務。

請仔細辨識圖片中的發票或收據，提取以下資訊並以 JSON 格式回傳：
{
  "vendor": "商家/供應商名稱",
  "items": [{"name": "品項名稱", "quantity": 數量, "unit_price": 單價, "amount": 小計}],
  "total_amount": 總金額,
  "tax_amount": 稅額,
  "invoice_number": "發票號碼",
  "date": "YYYY-MM-DD",
  "payment_method": "現金/刷卡/轉帳/LINE Pay/未指定",
  "category": "教材費/房租水電/人事費用/設備維護/辦公用品/餐飲點心/交通費/廣告行銷/保險稅務/其他支出",
  "raw_text": "辨識全文",
  "confidence": 0.0-1.0
}

辨識規則：
1. 金額務必為數字，去除千分位逗號
2. 若圖片模糊或無法辨識某欄位，該欄位回傳 null
3. 台灣統一發票格式：注意「營業人」「統一編號」「品名」「金額」等欄位
4. 手寫收據：盡力辨識手寫文字
5. payment_method 看不出來時填「未指定」，不要自創其他值
6. category 依商家與品項判斷（手搖飲、餐廳、超商食品 → 餐飲點心；文具、影印 → 辦公用品；書籍、講義 → 教材費），
   真的無法判斷才填「其他支出」
7. 若圖片不是發票/收據，回傳 {"error": "非發票或收據圖片", "raw_text": "辨識到的文字"}

請只回傳 JSON，不要包含 markdown。"""


class OCREngine:
    """使用 Gemini Vision API 進行發票/收據 OCR 辨識。"""

    def __init__(self) -> None:
        self.api_key = llm_config.api_key
        self.model = llm_config.vision_model
        self.enabled = bool(self.api_key)

        if self.enabled:
            logger.info("OCR Engine initialized with model: %s", self.model)
        else:
            logger.warning("OCR Engine disabled: GEMINI_API_KEY not configured.")

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
                    timeout=60,
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                is_last_attempt = attempt == MAX_RETRIES
                logger.warning(
                    "Gemini Vision request failed on attempt %d: %s", attempt + 1, exc
                )
                if is_last_attempt:
                    logger.error("Gemini Vision API failed after %d retries: %s", MAX_RETRIES, exc)
                    return None
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))

        return None

    def recognize(self, image_bytes: bytes) -> Optional[dict]:
        """
        辨識發票/收據圖片。

        Args:
            image_bytes: 圖片的二進位資料

        Returns:
            結構化的辨識結果 dict，或 None（辨識失敗）
        """
        if not self.enabled:
            return None

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": OCR_SYSTEM_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": image_b64,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2000,
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
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None

            text = parts[0].get("text", "").strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])

            result = json.loads(text)
            logger.info("OCR result: %s", json.dumps(result, ensure_ascii=False)[:300])
            return result

        except json.JSONDecodeError as exc:
            logger.error("Failed to parse OCR JSON: %s", exc)
            return None

    def is_available(self) -> bool:
        return self.enabled
