import os
from supabase import create_client

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY", "")

# ---- Phase 3: job-alert email inbox (all optional; empty = feature simply off) ----
ALERT_IMAP_HOST = os.environ.get("ALERT_IMAP_HOST", "imap.gmail.com")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
ALERT_EMAIL_APP_PASSWORD = os.environ.get("ALERT_EMAIL_APP_PASSWORD", "")

GEMINI_MODEL = "gemini-2.5-flash"          # fallback only; llm.py discovers models at runtime

# ---- Budgets ----
MAX_LLM_SCORES_PER_RUN = 60                # total scoring batches per run (whole system)
MAX_LLM_BATCHES_PER_USER = 4               # fairness: one user can't eat the whole budget
MAX_MATCH_MESSAGES_PER_USER = 8            # per run, avoid spamming a chat
MAX_JOBS_PER_USER_PER_RUN = 400            # cap on jobs considered per user per run
NEW_USER_BACKFILL = 150                    # how many existing jobs a brand-new user sees

# ---- Phase 2: apply guardrails ----
DEFAULT_DAILY_APPLY_CAP = 10               # quality over volume; per user per day
COVER_LETTER_MAX_WORDS = 140
APPLY_COOLDOWN_DAYS = 30                   # don't re-surface the same company+title
MAX_EMAILS_PER_RUN = 40                    # alert emails parsed per run

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


# ---------- per-user matching cursor (replaces the single global checkpoint) ----------

def get_cursor(user_id: int) -> int:
    r = sb.table("user_cursor").select("last_job_id").eq("user_id", user_id).execute()
    return int(r.data[0]["last_job_id"]) if r.data else -1   # -1 means "never seen before"


def set_cursor(user_id: int, last_job_id: int) -> None:
    sb.table("user_cursor").upsert({
        "user_id": user_id,
        "last_job_id": last_job_id,
        "updated_at": "now",
    }).execute()
