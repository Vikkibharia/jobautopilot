"""Phase 2 — the "apply" half.

READ THIS BEFORE EXPECTING FULLY-UNATTENDED SUBMISSION
------------------------------------------------------
The Phase 1 roadmap assumed Greenhouse and Lever expose public endpoints a
candidate can POST an application to. They do not, and this is the one place the
original design was factually wrong:

  * Greenhouse's application-submission endpoint
    (POST /v1/boards/{board}/jobs/{id}) authenticates with the EMPLOYER's Job Board
    API key. You, as a candidate, don't have it; unauthenticated POSTs are rejected.
  * Lever's apply endpoint is likewise keyed to the employer's account.
  * Ashby has no public candidate-side submit API at all.

So the only technical route to unattended submission is driving the public web form
with a headless browser. That is possible, and `apply_tier2.py` scaffolds it — but
(a) most ATS and employer terms forbid it, (b) Cloudflare/reCAPTCHA breaks it
unpredictably, and (c) employers increasingly detect and bin obviously-botted
applications, so it often lowers your hit rate rather than raising it.

What this module therefore does, and why it's actually better:
ASSISTED APPLY. The bot does the 95% that is tedious — decide the job is worth it,
draft a tailored cover letter, pull every screening answer from your own answer
bank, check caps/blocklist/cooldown, and hand you a single tap-through link. You do
the 5% that must be human: the final submit. Time per application drops from ~12
minutes to well under one, it works on EVERY platform including LinkedIn and Naukri,
and nothing can ever be sent in your name that you didn't see.
"""

import json
from datetime import datetime, timedelta, timezone

from .config import sb, DEFAULT_DAILY_APPLY_CAP, APPLY_COOLDOWN_DAYS, log_event
from .llm import cover_letter

# The answer bank is collected in ONE round trip. A conversational wizard would take
# hours on a 30-minute cron, so the bot sends a template, the user edits and sends it back.
ANSWER_TEMPLATE = """/answers
full_name: Your Name
email: you@example.com
phone: +91 90000 00000
current_location: Bengaluru
willing_to_relocate: Yes - Mumbai, Pune
notice_period: 30 days
current_ctc: 6.5 LPA
expected_ctc: 9-11 LPA
total_experience: 1.5 years
highest_qualification: MBA Marketing, 2024
work_authorization: Indian citizen, no sponsorship needed
linkedin: https://linkedin.com/in/yourhandle
portfolio: (leave blank if none)
why_looking: Want a role that combines marketing ops with analytics
availability_to_interview: Weekday evenings, weekends anytime"""

REQUIRED_KEYS = ("full_name", "email", "phone", "notice_period", "expected_ctc")


def parse_answer_block(text: str) -> dict:
    """Parse the `key: value` block the user sends back. Forgiving about spacing,
    case and stray blank lines; ignores the leading /answers command."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("/"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower().replace(" ", "_").replace("-", "_")
        v = v.strip()
        if k and v and not v.startswith("("):        # "(leave blank if none)" -> skip
            out[k] = v[:400]
    return out


def get_answers(user_id: int) -> dict:
    r = sb.table("answer_bank").select("answers").eq("user_id", user_id).execute()
    return (r.data[0]["answers"] if r.data else {}) or {}


def save_answers(user_id: int, answers: dict, cv_file_id: str | None = None,
                 cv_file_name: str | None = None) -> None:
    row = {"user_id": user_id, "answers": answers, "updated_at": "now"}
    if cv_file_id:
        row["cv_file_id"] = cv_file_id
        row["cv_file_name"] = cv_file_name or "cv.pdf"
    sb.table("answer_bank").upsert(row).execute()


def missing_keys(answers: dict) -> list[str]:
    return [k for k in REQUIRED_KEYS if not answers.get(k)]


# ------------------------------- guardrails -------------------------------

def applications_today(user_id: int) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        r = (sb.table("applications").select("id", count="exact")
             .eq("user_id", user_id).gte("created_at", since).execute())
        return r.count or 0
    except Exception:
        return 0


def is_blocked(user: dict, job: dict) -> str | None:
    """Returns a human-readable reason to skip, or None to proceed."""
    company = (job.get("company") or "").lower()
    for entry in (user.get("blocklist") or []):
        if entry and entry.lower() in company:
            return f"company is on your blocklist ({entry})"

    # Per-role cooldown: never surface the same company+title twice in a month.
    since = (datetime.now(timezone.utc) - timedelta(days=APPLY_COOLDOWN_DAYS)).isoformat()
    try:
        r = (sb.table("applications").select("id, payload")
             .eq("user_id", user["id"]).gte("created_at", since).limit(200).execute())
        for row in (r.data or []):
            p = row.get("payload") or {}
            if ((p.get("company") or "").lower() == company
                    and (p.get("title") or "").lower() == (job.get("title") or "").lower()):
                return "you already applied to this role in the last 30 days"
    except Exception:
        pass
    return None


def cap_reached(user: dict) -> bool:
    cap = int(user.get("daily_apply_cap") or DEFAULT_DAILY_APPLY_CAP)
    return applications_today(user["id"]) >= cap


# ------------------------------- the package -------------------------------

def build_package(user: dict, job: dict, score: int, rationale: str) -> tuple[str, str]:
    """Returns (checklist_text, cover_letter_text). Only ever uses the user's own
    profile + answer bank as source material."""
    answers = get_answers(user["id"])
    profile = user.get("profile") or {}

    try:
        letter = cover_letter(profile, job, answers)
    except Exception as e:
        log_event("cover_letter_error", {"user_id": user["id"], "error": str(e)[:300]})
        letter = ""

    order = ["full_name", "email", "phone", "current_location", "total_experience",
             "current_ctc", "expected_ctc", "notice_period", "willing_to_relocate",
             "work_authorization", "highest_qualification", "linkedin", "portfolio"]
    lines = [f"• {k.replace('_', ' ').title()}: {answers[k]}" for k in order if answers.get(k)]
    for k, v in answers.items():
        if k not in order:
            lines.append(f"• {k.replace('_', ' ').title()}: {v}")

    gaps = missing_keys(answers)
    checklist = "📋 Copy-paste answers for the form:\n" + ("\n".join(lines) or "(answer bank empty)")
    if gaps:
        checklist += "\n\n⚠️ Missing from your bank: " + ", ".join(gaps) + " — send /answers to fix."
    return checklist, letter


def record_application(user_id: int, match_id: int, job: dict, method: str,
                       cover: str = "", tier: int = 1) -> None:
    """Audit-first: the snapshot is written when the package is handed over, so there
    is always a record of exactly what was put in front of you (or submitted)."""
    try:
        sb.table("applications").insert({
            "user_id": user_id,
            "match_id": match_id,
            "tier": tier,
            "method": method,
            "cover_letter": cover[:4000] or None,
            "payload": {"title": job.get("title"), "company": job.get("company"),
                        "location": job.get("location"), "url": job.get("url"),
                        "source": job.get("source"), "ats": job.get("ats")},
            "submitted_at": "now" if method != "assisted" else None,
            "outcome": "package_ready" if method == "assisted" else "submitted",
        }).execute()
    except Exception as e:
        log_event("application_record_error", {"error": str(e)[:300]})


def mark_match(match_id: int, status: str) -> None:
    try:
        sb.table("matches").update({"status": status}).eq("id", match_id).execute()
    except Exception:
        pass
