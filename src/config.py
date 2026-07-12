import os
from supabase import create_client

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY", "")

GEMINI_MODEL = "gemini-2.5-flash"
MAX_LLM_SCORES_PER_RUN = 60          # protects the free quota across all users
MAX_MATCH_MESSAGES_PER_USER = 8      # per run, avoid spamming a chat

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def get_state(key: str, default: str = "") -> str:
    r = sb.table("state").select("value").eq("key", key).execute()
    return r.data[0]["value"] if r.data else default


def set_state(key: str, value: str) -> None:
    sb.table("state").upsert({"key": key, "value": value}).execute()


def log_event(kind: str, detail: dict) -> None:
    try:
        sb.table("events").insert({"kind": kind, "detail": detail}).execute()
    except Exception:
        pass
