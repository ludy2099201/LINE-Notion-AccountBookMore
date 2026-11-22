"""Webhook 端到端測試（LINE / Notion 皆以替身取代）。

app.main 在 import 時會讀環境變數並驗證，所以放在子行程裡跑，
避免影響其他測試所用的設定。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r'''
import base64, hashlib, hmac, json, threading
import app.main as m

calls, sent = [], []
done = threading.Event()

class FakeBookkeeper:
    def handle_text(self, text, user_name, is_redelivery=False, event_time=None):
        calls.append({
            "text": text, "user": user_name, "redelivery": is_redelivery,
            "event_time": event_time.isoformat() if event_time else None,
        })
        return "回覆：" + text

def reply(token, text):
    sent.append(text)
    done.set()
    return True

m.bookkeeper = FakeBookkeeper()
m.line_handler.reply_message = reply
m.line_handler.get_user_profile = lambda user_id: {"displayName": "Mia"}

def post(event):
    body = json.dumps({"events": [event]}, ensure_ascii=False).encode()
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    done.clear()
    response = m.app.test_client().post("/webhook", data=body, headers={"X-Line-Signature": signature})
    assert response.status_code == 200, response.status_code
    assert done.wait(5), "no reply sent"

base = {"type": "message", "replyToken": "t", "source": {"userId": "U1"},
        "message": {"type": "text", "text": "/支 講義 4500"}}
post({**base, "timestamp": 1795331140000, "deliveryContext": {"isRedelivery": True}})
post({**base})
print(json.dumps({"calls": calls, "sent": sent}, ensure_ascii=False))
'''


def test_text_event_reaches_bookkeeper_and_reply_is_sent():
    env = {
        **os.environ,
        "LINE_CHANNEL_ACCESS_TOKEN": "token",
        "LINE_CHANNEL_SECRET": "secret",
        "NOTION_API_KEY": "key",
        "NOTION_DATABASE_ID": "2856d80f-b494-4965-9e1e-9c7700cb0edf",
        "GEMINI_API_KEY": "key",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    output = json.loads(result.stdout.strip().splitlines()[-1])

    redelivered, plain = output["calls"]
    assert redelivered == {
        "text": "/支 講義 4500", "user": "Mia", "redelivery": True,
        "event_time": "2026-11-22T07:05:40+00:00",
    }
    assert plain["redelivery"] is False and plain["event_time"] is None
    assert output["sent"] == ["回覆：/支 講義 4500"] * 2
