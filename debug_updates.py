# ===== کتابخانه‌ها =====
import os
import json
import requests

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ===== خواندن state =====
STATE_FILE = "paper_state.json"
last_id = 0
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
        last_id = state.get("last_update_id", 0)

print(f"=== DEBUG UPDATES ===")
print(f"Last update_id in state: {last_id}")
print(f"Offset for next getUpdates: {last_id + 1}")

# ===== دریافت همه updates =====
url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
r = requests.get(url, params={"timeout": 0}, timeout=15)
data = r.json()

print(f"\nStatus: {r.status_code}")
print(f"OK: {data.get('ok')}")

updates = data.get("result", [])
print(f"Total updates: {len(updates)}")

for upd in updates:
    upd_id = upd.get("update_id", "?")
    print(f"\n--- Update ID: {upd_id} ---")
    print(f"Keys: {list(upd.keys())}")

    if "callback_query" in upd:
        cb = upd["callback_query"]
        print(f"  CALLBACK DATA: {cb.get('data')}")
        print(f"  CALLBACK ID: {cb.get('id')}")
        print(f"  Message ID: {cb['message']['message_id']}")

    if "message" in upd:
        msg = upd["message"]
        print(f"  MESSAGE TEXT: {msg.get('text', '')[:100]}")