import requests
from .config import TELEGRAM_BOT_TOKEN, get_state, set_state

BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send(chat_id: int, text: str) -> None:
    for chunk_start in range(0, len(text), 3900):
        requests.post(
            f"{BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text[chunk_start:chunk_start + 3900],
                  "disable_web_page_preview": True},
            timeout=20,
        )


def get_updates() -> list[dict]:
    offset = int(get_state("tg_offset", "0") or 0)
    r = requests.get(f"{BASE}/getUpdates", params={"offset": offset + 1, "timeout": 0}, timeout=30)
    updates = r.json().get("result", [])
    if updates:
        set_state("tg_offset", str(updates[-1]["update_id"]))
    return updates


def download_document(file_id: str) -> bytes:
    meta = requests.get(f"{BASE}/getFile", params={"file_id": file_id}, timeout=20).json()
    path = meta["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=60).content
