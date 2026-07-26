import requests
from .config import TELEGRAM_BOT_TOKEN, get_state, set_state, log_event

BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send(chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> bool:
    """Returns False if Telegram rejected the send (e.g. user blocked the bot),
    so callers can deactivate dead chats instead of retrying forever."""
    ok = True
    chunks = [text[i:i + 3900] for i in range(0, max(len(text), 1), 3900)] or [text]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if buttons and idx == len(chunks) - 1:      # buttons only on the final chunk
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = requests.post(f"{BASE}/sendMessage", json=payload, timeout=20)
            if r.status_code != 200:
                ok = False
                log_event("tg_send_failed", {"chat_id": chat_id, "status": r.status_code,
                                             "body": r.text[:300]})
        except Exception as e:
            ok = False
            log_event("tg_send_failed", {"chat_id": chat_id, "error": str(e)[:300]})
    return ok


def answer_callback(callback_id: str, text: str = "") -> None:
    """Stops the spinner on a tapped inline button."""
    try:
        requests.post(f"{BASE}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text[:200]}, timeout=20)
    except Exception:
        pass


def edit_markup(chat_id: int, message_id: int, buttons: list[list[dict]] | None) -> None:
    """Replace the buttons under an already-sent message (e.g. grey out Applied)."""
    try:
        requests.post(f"{BASE}/editMessageReplyMarkup",
                      json={"chat_id": chat_id, "message_id": message_id,
                            "reply_markup": {"inline_keyboard": buttons or []}}, timeout=20)
    except Exception:
        pass


def get_updates() -> list[dict]:
    """NOTE: the offset is advanced by main.py AFTER each update is handled
    (see commit_offset), not here. In Phase 1 the offset moved before processing,
    so a crash mid-loop silently ate the user's CV upload."""
    offset = int(get_state("tg_offset", "0") or 0)
    try:
        r = requests.get(f"{BASE}/getUpdates",
                         params={"offset": offset + 1, "timeout": 0,
                                 "allowed_updates": '["message","callback_query"]'},
                         timeout=30)
        return r.json().get("result", [])
    except Exception as e:
        log_event("tg_getupdates_failed", {"error": str(e)[:300]})
        return []


def commit_offset(update_id: int) -> None:
    set_state("tg_offset", str(update_id))


def download_document(file_id: str) -> bytes:
    meta = requests.get(f"{BASE}/getFile", params={"file_id": file_id}, timeout=20).json()
    path = meta["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}",
                        timeout=60).content
