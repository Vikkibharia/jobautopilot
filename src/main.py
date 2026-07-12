import io
import re
from pypdf import PdfReader
from .config import sb, get_state, set_state, log_event, MAX_LLM_SCORES_PER_RUN, MAX_MATCH_MESSAGES_PER_USER
from . import telegram as tg
from . import ingest
from .llm import parse_cv, score_jobs

HELP = (
    "🤖 JobAutopilot\n"
    "Send your CV as a PDF file and I'll start finding matching jobs across India every ~30 min.\n\n"
    "Commands:\n/status — your profile summary\n/threshold 70 — set match cutoff (default 75)\n"
    "/pause · /resume — stop/start matching\n/help — this message"
)


# ---------- Telegram update handling (registration, CV upload, commands) ----------

def _get_user(chat_id: int):
    r = sb.table("users").select("*").eq("telegram_chat_id", chat_id).execute()
    return r.data[0] if r.data else None


def handle_updates() -> None:
    for u in tg.get_updates():
        msg = u.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id:
            continue
        user = _get_user(chat_id)
        text = (msg.get("text") or "").strip()
        doc = msg.get("document")

        if text.startswith("/start"):
            if not user:
                sb.table("users").insert({
                    "telegram_chat_id": chat_id,
                    "name": (msg.get("from") or {}).get("first_name", ""),
                }).execute()
            tg.send(chat_id, "👋 Welcome! " + HELP)

        elif doc:
            if not (doc.get("file_name", "").lower().endswith(".pdf") or doc.get("mime_type") == "application/pdf"):
                tg.send(chat_id, "Please send your CV as a PDF file.")
                continue
            if not user:
                sb.table("users").insert({"telegram_chat_id": chat_id,
                                          "name": (msg.get("from") or {}).get("first_name", "")}).execute()
            tg.send(chat_id, "📄 Got your CV — parsing it now…")
            try:
                pdf = PdfReader(io.BytesIO(tg.download_document(doc["file_id"])))
                cv_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
                if len(cv_text.strip()) < 100:
                    tg.send(chat_id, "I couldn't read text from that PDF (it may be a scanned image). Please send a text-based PDF.")
                    continue
                profile = parse_cv(cv_text)
                sb.table("users").update({"profile": profile, "active": True,
                                          "updated_at": "now()"}).eq("telegram_chat_id", chat_id).execute()
                tg.send(chat_id,
                        f"✅ Profile saved!\n\n👤 {profile.get('current_title')} · {profile.get('years_experience')} yrs · level: {profile.get('level')}\n"
                        f"🛠 Skills: {', '.join(profile.get('skills', [])[:10])}\n"
                        f"📍 Locations: {', '.join(profile.get('locations', []))}\n"
                        f"🎯 Watching for: {', '.join((profile.get('lateral_titles') or [])[:3])} + next-step roles like {', '.join((profile.get('next_step_titles') or [])[:3])}\n\n"
                        "Matching jobs will start arriving from the next scan (within ~30 min).")
            except Exception as e:
                log_event("cv_parse_error", {"chat_id": chat_id, "error": str(e)})
                tg.send(chat_id, "Sorry — something went wrong parsing that CV. Please try again.")

        elif text.startswith("/status"):
            if user and user.get("profile"):
                p = user["profile"]
                tg.send(chat_id, f"👤 {p.get('current_title')} ({p.get('level')}) · threshold {user['threshold']} · {'active ✅' if user['active'] else 'paused ⏸'}")
            else:
                tg.send(chat_id, "No CV on file yet — send it as a PDF.")

        elif text.startswith("/threshold"):
            m = re.search(r"(\d{2,3})", text)
            if user and m and 40 <= int(m.group(1)) <= 100:
                sb.table("users").update({"threshold": int(m.group(1))}).eq("telegram_chat_id", chat_id).execute()
                tg.send(chat_id, f"🎚 Match threshold set to {m.group(1)}.")
            else:
                tg.send(chat_id, "Usage: /threshold 70  (between 40 and 100)")

        elif text.startswith("/pause") and user:
            sb.table("users").update({"active": False}).eq("telegram_chat_id", chat_id).execute()
            tg.send(chat_id, "⏸ Paused. /resume to restart.")

        elif text.startswith("/resume") and user:
            sb.table("users").update({"active": True}).eq("telegram_chat_id", chat_id).execute()
            tg.send(chat_id, "▶️ Resumed.")

        elif text:
            tg.send(chat_id, HELP)


# ---------- Matching ----------

def _prefilter(profile: dict, job: dict) -> bool:
    """Cheap keyword gate so the free LLM quota is spent only on plausible fits."""
    hay = f"{job['title']} {job.get('description','')}".lower()
    skills = [s.lower() for s in profile.get("skills", [])]
    titles = [t.lower() for t in (profile.get("lateral_titles", []) + profile.get("next_step_titles", []))]
    skill_hits = sum(1 for s in skills if s and s in hay)
    title_hit = any(t and t in job["title"].lower() for t in titles)
    return title_hit or skill_hits >= 3


def run_matching() -> None:
    users = sb.table("users").select("*").eq("active", True).not_.is_("profile", "null").execute().data
    if not users:
        return
    last_job_id = int(get_state("last_matched_job_id", "0") or 0)
    jobs = (sb.table("jobs").select("id,title,company,location,description,url,ats")
            .gt("id", last_job_id).order("id").limit(400).execute().data)
    if not jobs:
        return
    set_state("last_matched_job_id", str(jobs[-1]["id"]))

    llm_budget = MAX_LLM_SCORES_PER_RUN
    for user in users:
        profile, chat_id = user["profile"], user["telegram_chat_id"]
        candidates = [j for j in jobs if _prefilter(profile, j)]
        sent = 0
        for i in range(0, len(candidates), 8):
            if llm_budget <= 0 or sent >= MAX_MATCH_MESSAGES_PER_USER:
                break
            batch = candidates[i:i + 8]
            llm_budget -= 1
            try:
                results = score_jobs(profile, batch)
            except Exception as e:
                log_event("score_error", {"error": str(e)})
                continue
            for res in results:
                try:
                    job = batch[int(res["index"])]
                    score = int(res["score"])
                except Exception:
                    continue
                if score < user["threshold"] or sent >= MAX_MATCH_MESSAGES_PER_USER:
                    continue
                try:
                    sb.table("matches").insert({
                        "user_id": user["id"], "job_id": job["id"], "score": score,
                        "classification": res.get("classification", "lateral"),
                        "rationale": res.get("rationale", ""),
                    }).execute()
                except Exception:
                    continue  # already matched before
                badge = "⬆️ NEXT STEP" if res.get("classification") == "next_step" else "↔️ Lateral"
                tg.send(chat_id,
                        f"🎯 {score}/100 · {badge}\n"
                        f"{job['title']} — {job.get('company','')}\n"
                        f"📍 {job.get('location','')}\n"
                        f"💡 {res.get('rationale','')}\n"
                        f"🔗 {job['url']}")
                sent += 1


def main() -> None:
    handle_updates()      # registrations, CV uploads, commands since last run
    ingest.ingest_all()   # pull fresh jobs
    run_matching()        # score new jobs for every active user & notify
    handle_updates()      # catch anything sent during the run


if __name__ == "__main__":
    main()
