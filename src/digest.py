"""Self-monitoring for a non-coder operator.

Two reports, both delivered in Telegram so Supabase and GitHub never need to be
opened for routine checks:

  * Daily health report  — did the pipeline actually work in the last 24h?
    Jobs per source, matches sent, errors in plain words, dead seed slugs,
    and an early warning before GitHub's 60-day cron auto-disable.
  * Weekly tuning digest — per user: what arrived, what you did with it, and
    concrete /threshold //watch suggestions derived from your own taps.

Both are cheap: one state-table read per run to decide "not due yet"."""

import os
from datetime import datetime, timedelta, timezone

import requests

from .config import (sb, get_state, set_state, log_event, ADMIN_CHAT_ID,
                     ADZUNA_APP_ID, ADZUNA_APP_KEY, CAREERJET_AFFID, JOOBLE_API_KEY,
                     ALERT_EMAIL)
from . import telegram as tg

# Send after 03:00 UTC = 08:30 IST, so the report covers a full night of runs.
SEND_AFTER_UTC_HOUR = 3
CRON_DISABLE_WARN_DAYS = 45      # GitHub disables cron ~60 days after the last commit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recipients() -> list[int]:
    if ADMIN_CHAT_ID:
        try:
            return [int(ADMIN_CHAT_ID)]
        except ValueError:
            pass
    rows = (sb.table("users").select("telegram_chat_id")
            .eq("active", True).execute().data) or []
    return [r["telegram_chat_id"] for r in rows]


# ------------------------------- daily health -------------------------------

def _repo_age_warning() -> str:
    """GitHub switches the schedule off after ~60 days without a commit. Warn early."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return ""
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/commits?per_page=1",
                         timeout=15).json()
        last = datetime.fromisoformat(
            r[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
        days = (_now() - last).days
        if days >= CRON_DISABLE_WARN_DAYS:
            return (f"\n⏰ No commit to the repo in {days} days. GitHub switches the "
                    "schedule OFF at ~60 days — open any file on GitHub, make a tiny "
                    "edit (add a space), and Commit changes to reset the clock.")
    except Exception:
        pass
    return ""


def run_daily_health() -> None:
    now = _now()
    if now.hour < SEND_AFTER_UTC_HOUR:
        return
    today = now.date().isoformat()
    if get_state("last_health_date") == today:
        return
    set_state("last_health_date", today)     # set first: a crash must not cause spam

    since = (now - timedelta(hours=24)).isoformat()
    events = (sb.table("events").select("kind,detail")
              .gte("created_at", since).limit(2000).execute().data) or []

    runs, by_source, errors, dead_slugs = 0, {}, {}, set()
    for e in events:
        kind, detail = e.get("kind", ""), e.get("detail") or {}
        if kind == "ingest_done":
            runs += 1
            for s, n in (detail.get("by_source") or {}).items():
                by_source[s] = by_source.get(s, 0) + (n or 0)
        elif kind == "ingest_error":
            src = str(detail.get("source", ""))
            errors[src or "ingest"] = errors.get(src or "ingest", 0) + 1
            # greenhouse:xyz / lever:xyz / ashby:xyz that error every run = dead slug
            if ":" in src:
                dead_slugs.add(src)
        elif kind.endswith("_error") or kind.endswith("_failed"):
            errors[kind] = errors.get(kind, 0) + 1

    matches24 = (sb.table("matches").select("id", count="exact")
                 .gte("created_at", since).execute().count) or 0

    src_line = " · ".join(
        f"{k.removeprefix('fetch_')} {v}"
        for k, v in sorted(by_source.items(), key=lambda x: -x[1])) or "none"

    warnings = []
    if ADZUNA_APP_ID and ADZUNA_APP_KEY and by_source.get("fetch_adzuna", 0) == 0:
        warnings.append("Adzuna keys are set but returned 0 all day — check the key.")
    if CAREERJET_AFFID and by_source.get("fetch_careerjet", 0) == 0:
        warnings.append("Careerjet ID is set but returned 0 all day — check the ID.")
    off = [n for n, k in (("Adzuna", ADZUNA_APP_ID and ADZUNA_APP_KEY),
                          ("Careerjet", CAREERJET_AFFID),
                          ("Jooble", JOOBLE_API_KEY),
                          ("Email alerts", ALERT_EMAIL)) if not k]
    if off:
        warnings.append("Off (no key yet): " + ", ".join(off) + ".")
    if dead_slugs:
        warnings.append("Dead careers-page slugs (remove or fix them in "
                        "seed_companies.json): " + ", ".join(sorted(dead_slugs)[:8]))
    if runs == 0:
        warnings.append("No pipeline runs recorded in 24h — check GitHub → Actions.")

    body = (f"🩺 Daily health — last 24h\n"
            f"🔄 Runs: {runs}\n"
            f"📥 New jobs: {sum(by_source.values())}\n"
            f"   {src_line}\n"
            f"🎯 Matches sent: {matches24}")
    if errors:
        top = sorted(errors.items(), key=lambda x: -x[1])[:3]
        body += "\n⚠️ Errors: " + ", ".join(f"{k} ×{v}" for k, v in top)
    if warnings:
        body += "\n\n" + "\n".join("❗ " + w for w in warnings)
    body += _repo_age_warning()
    body += "\n\nAll good? Then no action needed — this is just your daily pulse."

    for chat_id in _recipients():
        tg.send(chat_id, body)


# ------------------------------- weekly tuning digest -------------------------------

def _suggestions(user: dict, total: int, applied: int, skipped: int,
                 skips_by_source: dict) -> list[str]:
    out = []
    thr = user.get("threshold", 75)
    if total == 0:
        out.append(f"No matches at all this week. Try /threshold {max(40, thr - 5)} "
                   "or add more titles with /watch — those titles are also the "
                   "search query sent to the job APIs.")
        return out
    act_rate = (applied + skipped) / total
    if total >= 15 and applied / total < 0.1:
        out.append(f"You applied to under 10% of matches. Raise the bar: "
                   f"/threshold {min(95, thr + 5)} cuts the noise.")
    if act_rate < 0.3 and total >= 10:
        out.append("Most matches got no tap at all. Tapping ✅ Applied / 🚫 Skip is "
                   "what teaches the system — 2 seconds per match keeps these "
                   "suggestions honest.")
    noisy = [(s, n) for s, n in skips_by_source.items() if n >= 4]
    for s, n in sorted(noisy, key=lambda x: -x[1])[:1]:
        out.append(f"You skipped {n} matches from '{s}' — if that source keeps "
                   "missing, tighten /watch to more specific titles.")
    if not out:
        out.append("Settings look healthy. The metric that matters is replies per "
                   "application — if 15+ applications get zero replies, the CV or "
                   "targeting needs work, not the bot.")
    return out


def run_weekly_digest() -> None:
    now = _now()
    if now.weekday() != 0 or now.hour < SEND_AFTER_UTC_HOUR:   # Mondays, after 08:30 IST
        return
    week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
    if get_state("last_digest_week") == week:
        return
    set_state("last_digest_week", week)

    since = (now - timedelta(days=7)).isoformat()
    users = (sb.table("users").select("*")
             .eq("active", True).not_.is_("profile", "null").execute().data) or []

    for user in users:
        rows = (sb.table("matches").select("status, jobs(source)")
                .eq("user_id", user["id"]).gte("created_at", since)
                .limit(500).execute().data) or []
        total = len(rows)
        applied = sum(1 for r in rows if r.get("status") == "applied")
        skipped = sum(1 for r in rows if r.get("status") == "skipped")
        idle = total - applied - skipped
        skips_by_source = {}
        for r in rows:
            if r.get("status") == "skipped":
                s = ((r.get("jobs") or {}).get("source") or "?").replace("email:", "")
                skips_by_source[s] = skips_by_source.get(s, 0) + 1

        tips = _suggestions(user, total, applied, skipped, skips_by_source)
        body = (f"📈 Your week in review\n"
                f"🎯 Matches: {total} · ✅ Applied: {applied} · "
                f"🚫 Skipped: {skipped} · 😴 No action: {idle}\n"
                f"🎚 Threshold {user.get('threshold', 75)} · "
                f"apply mode: {user.get('apply_mode', 'off')}\n\n"
                + "\n".join("💡 " + t for t in tips))
        tg.send(user["telegram_chat_id"], body)
