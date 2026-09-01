import io
import re
from pypdf import PdfReader

from .config import (sb, get_cursor, set_cursor, log_event, MAX_LLM_SCORES_PER_RUN,
                     MAX_LLM_BATCHES_PER_USER, MAX_MATCH_MESSAGES_PER_USER,
                     MAX_JOBS_PER_USER_PER_RUN, NEW_USER_BACKFILL)
from . import telegram as tg
from . import ingest
from . import apply as ap
from . import digest
from .llm import parse_cv, score_jobs

HELP = (
    "🤖 JobAutopilot\n"
    "Send your CV as a PDF and I'll find matching jobs across India every ~30 min.\n\n"
    "MATCHING\n/status — your profile · /threshold 70 — match cutoff\n"
    "/watch analyst, marketing manager — override the titles I hunt for\n"
    "/level mid — fix your seniority if I read the CV wrong\n"
    "/pause · /resume\n\n"
    "APPLYING\n/answers — set up your one-time answer bank (needed before applying)\n"
    "/apply on — get a ready-to-submit package with every match\n/apply off — links only\n"
    "/cap 8 — max applications per day (default 10)\n"
    "/block acme corp — never show me this company\n"
    "/stats — what I've found and what you've applied to\n\n"
    "Tap ✅ Applied under a match and I'll track it for you."
)


def _get_user(chat_id: int):
    r = sb.table("users").select("*").eq("telegram_chat_id", chat_id).execute()
    return r.data[0] if r.data else None


def _get_user_by_id(user_id: int):
    r = sb.table("users").select("*").eq("id", user_id).execute()
    return r.data[0] if r.data else None


def _register(chat_id: int, first_name: str) -> dict:
    """New users start their cursor near the top of the jobs table with a small
    backfill window, so they get a taste immediately without a flood of stale posts."""
    sb.table("users").insert({"telegram_chat_id": chat_id, "name": first_name}).execute()
    user = _get_user(chat_id)
    try:
        top = sb.table("jobs").select("id").order("id", desc=True).limit(1).execute().data
        max_id = int(top[0]["id"]) if top else 0
        set_cursor(user["id"], max(0, max_id - NEW_USER_BACKFILL))
    except Exception:
        pass
    return user


def _match_buttons(match_id: int, url: str) -> list[list[dict]]:
    return [
        [{"text": "🔗 Open & apply", "url": url}],
        [{"text": "📝 Cover letter", "callback_data": f"cl:{match_id}"},
         {"text": "✅ Applied", "callback_data": f"ap:{match_id}"},
         {"text": "🚫 Skip", "callback_data": f"sk:{match_id}"}],
    ]


# ---------------- inline-button handling (the apply tracker) ----------------

def _handle_callback(cq: dict) -> None:
    data = cq.get("data") or ""
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    message_id = (cq.get("message") or {}).get("message_id")
    cb_id = cq.get("id")
    if not (chat_id and ":" in data):
        return tg.answer_callback(cb_id)

    action, _, raw_id = data.partition(":")
    try:
        match_id = int(raw_id)
    except ValueError:
        return tg.answer_callback(cb_id)

    user = _get_user(chat_id)
    m = sb.table("matches").select("*").eq("id", match_id).execute().data
    if not (user and m):
        return tg.answer_callback(cb_id, "Not found")
    match = m[0]
    if match["user_id"] != user["id"]:            # isolation: never touch another user's row
        return tg.answer_callback(cb_id, "Not yours")

    job = sb.table("jobs").select("*").eq("id", match["job_id"]).execute().data
    job = job[0] if job else {}

    if action == "ap":
        ap.mark_match(match_id, "applied")
        ap.record_application(user["id"], match_id, job, method="manual")
        tg.answer_callback(cb_id, "Logged ✅")
        tg.edit_markup(chat_id, message_id, [[{"text": "✅ Applied — logged", "url": job.get("url", "https://t.me")}]])
    elif action == "sk":
        ap.mark_match(match_id, "skipped")
        tg.answer_callback(cb_id, "Skipped")
        tg.edit_markup(chat_id, message_id, None)
    elif action == "cl":
        tg.answer_callback(cb_id, "Writing…")
        checklist, letter = ap.build_package(user, job, match.get("score", 0),
                                            match.get("rationale", ""))
        body = f"✍️ For {job.get('title')} at {job.get('company')}\n\n"
        body += (letter or "(cover letter generation failed — try again next run)")
        tg.send(chat_id, body + "\n\n" + checklist)
        ap.record_application(user["id"], match_id, job, method="assisted", cover=letter)
    else:
        tg.answer_callback(cb_id)


# ---------------- message handling ----------------

def _handle_message(msg: dict) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        return
    user = _get_user(chat_id)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    doc = msg.get("document")
    first_name = (msg.get("from") or {}).get("first_name", "")

    if text.startswith("/start"):
        if not user:
            _register(chat_id, first_name)
        tg.send(chat_id, "👋 Welcome! " + HELP)
        return

    if doc:
        if not (doc.get("file_name", "").lower().endswith(".pdf")
                or doc.get("mime_type") == "application/pdf"):
            tg.send(chat_id, "Please send your CV as a PDF file.")
            return
        if not user:
            user = _register(chat_id, first_name)
        tg.send(chat_id, "📄 Got your CV — parsing it now…")
        try:
            pdf = PdfReader(io.BytesIO(tg.download_document(doc["file_id"])))
            cv_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            if len(cv_text.strip()) < 100:
                tg.send(chat_id, "I couldn't read text from that PDF (it may be a scanned "
                                 "image). Please send a text-based PDF.")
                return
            profile = parse_cv(cv_text)
            sb.table("users").update({"profile": profile, "active": True}) \
                .eq("telegram_chat_id", chat_id).execute()
            # Keep the file_id so Phase 2 can re-attach the CV without asking again.
            ap.save_answers(user["id"], ap.get_answers(user["id"]),
                            cv_file_id=doc["file_id"], cv_file_name=doc.get("file_name"))
            tg.send(chat_id,
                    f"✅ Profile saved!\n\n👤 {profile.get('current_title')} · "
                    f"{profile.get('years_experience')} yrs · level: {profile.get('level')}\n"
                    f"🧩 Skill families: {', '.join(profile.get('skill_families') or [])}\n"
                    f"🛠 Skills: {', '.join(profile.get('skills', [])[:10])}\n"
                    f"📍 Locations: {', '.join(profile.get('locations', []))}\n"
                    f"🎯 Watching: {', '.join((profile.get('lateral_titles') or [])[:4])}\n"
                    f"⬆️ Next step: {', '.join((profile.get('next_step_titles') or [])[:3])}\n\n"
                    "If any of that is wrong, fix it with /level and /watch — those two "
                    "control everything I search for.\n"
                    "Next: send /answers to set up one-tap applying.")
        except Exception as e:
            log_event("cv_parse_error", {"chat_id": chat_id, "error": str(e)[:400]})
            tg.send(chat_id, "Sorry — something went wrong parsing that CV. Please try again.")
        return

    if not user:
        user = _register(chat_id, first_name)

    # ---- answer bank ----
    if text.startswith("/answers"):
        parsed = ap.parse_answer_block(text)
        if not parsed:
            tg.send(chat_id, "Copy the block below, edit every line, and send it back as ONE "
                             "message. It's stored once and reused on every application — "
                             "and nothing outside it is ever put on a form.\n\n"
                             + ap.ANSWER_TEMPLATE)
            return
        merged = {**ap.get_answers(user["id"]), **parsed}
        ap.save_answers(user["id"], merged)
        gaps = ap.missing_keys(merged)
        tg.send(chat_id, f"💾 Saved {len(parsed)} answers ({len(merged)} on file)."
                + (f"\n⚠️ Still missing: {', '.join(gaps)}" if gaps
                   else "\n✅ Complete. Send /apply on to start getting ready-to-submit packages."))
        return

    if text.startswith("/apply"):
        mode = "assisted" if re.search(r"\bon\b", text, re.I) else \
               ("off" if re.search(r"\boff\b", text, re.I) else None)
        if mode is None:
            tg.send(chat_id, f"Apply mode is: {user.get('apply_mode', 'off')}. "
                             "Use /apply on or /apply off.")
            return
        if mode == "assisted" and ap.missing_keys(ap.get_answers(user["id"])):
            tg.send(chat_id, "First send /answers — I won't build an application package "
                             "from facts you haven't given me.")
            return
        sb.table("users").update({"apply_mode": mode}).eq("id", user["id"]).execute()
        tg.send(chat_id, "🚀 Apply mode ON. Every match now arrives with a tailored cover "
                         "letter and your form answers ready to paste."
                if mode == "assisted" else "🔕 Apply mode off — links only.")
        return

    if text.startswith("/watch"):
        titles = [t.strip() for t in text[len("/watch"):].split(",") if t.strip()]
        if not titles:
            p = user.get("profile") or {}
            tg.send(chat_id, "Currently hunting: "
                    + ", ".join((p.get("lateral_titles") or []) + (p.get("next_step_titles") or []))
                    + "\n\nOverride with: /watch marketing analyst, growth analyst, category manager")
            return
        profile = dict(user.get("profile") or {})
        profile["lateral_titles"] = titles[:10]
        # FIX: /watch is authoritative. Stale CV-derived next_step_titles used to keep
        # leaking into the prefilter and the Adzuna/Jooble search terms, so a bad CV
        # parse (e.g. the junior demotion) survived a /watch override.
        profile["next_step_titles"] = []
        sb.table("users").update({"profile": profile}).eq("id", user["id"]).execute()
        tg.send(chat_id, "🎯 Now hunting: " + ", ".join(titles[:10])
                + "\nThese also become the search terms I send to the job APIs.")
        return

    if text.startswith("/level"):
        m = re.search(r"(intern|junior|mid|senior|lead|staff|manager|director|executive)",
                      text, re.I)
        if not m:
            tg.send(chat_id, "Usage: /level mid  (intern·junior·mid·senior·lead·staff·"
                             "manager·director·executive)")
            return
        profile = dict(user.get("profile") or {})
        profile["level"] = m.group(1).lower()
        sb.table("users").update({"profile": profile}).eq("id", user["id"]).execute()
        tg.send(chat_id, f"📈 Level set to {m.group(1).lower()}.")
        return

    if text.startswith("/block"):
        name = text[len("/block"):].strip()
        if not name:
            tg.send(chat_id, "Blocklist: " + (", ".join(user.get("blocklist") or []) or "(empty)")
                    + "\nAdd with: /block acme corp")
            return
        bl = list(user.get("blocklist") or []) + [name[:120]]
        sb.table("users").update({"blocklist": bl}).eq("id", user["id"]).execute()
        tg.send(chat_id, f"🚫 Blocked: {name}")
        return

    if text.startswith("/cap"):
        m = re.search(r"(\d{1,2})", text)
        if m and 1 <= int(m.group(1)) <= 30:
            sb.table("users").update({"daily_apply_cap": int(m.group(1))}).eq("id", user["id"]).execute()
            tg.send(chat_id, f"🧢 Daily application cap: {m.group(1)}. "
                             "(Quality beats volume — 5 tailored beats 50 blind.)")
        else:
            tg.send(chat_id, "Usage: /cap 8  (1–30 per day)")
        return

    if text.startswith("/stats"):
        try:
            mt = sb.table("matches").select("id", count="exact").eq("user_id", user["id"]).execute()
            apd = sb.table("matches").select("id", count="exact") \
                .eq("user_id", user["id"]).eq("status", "applied").execute()
            tg.send(chat_id, f"📊 Matches surfaced: {mt.count or 0}\n"
                             f"✅ Applied: {apd.count or 0}\n"
                             f"📅 Apply packages in last 24h: {ap.applications_today(user['id'])}"
                             f" / cap {user.get('daily_apply_cap', 10)}\n"
                             f"🎚 Threshold: {user['threshold']} · Apply mode: "
                             f"{user.get('apply_mode', 'off')}")
        except Exception:
            tg.send(chat_id, "Couldn't read your stats this run — try again next cycle.")
        return

    if text.startswith("/status"):
        if user.get("profile"):
            p = user["profile"]
            tg.send(chat_id, f"👤 {p.get('current_title')} ({p.get('level')}) · threshold "
                             f"{user['threshold']} · apply: {user.get('apply_mode', 'off')} · "
                             f"{'active ✅' if user['active'] else 'paused ⏸'}")
        else:
            tg.send(chat_id, "No CV on file yet — send it as a PDF.")
        return

    if text.startswith("/threshold"):
        m = re.search(r"(\d{2,3})", text)
        if m and 40 <= int(m.group(1)) <= 100:
            sb.table("users").update({"threshold": int(m.group(1))}).eq("id", user["id"]).execute()
            tg.send(chat_id, f"🎚 Match threshold set to {m.group(1)}.")
        else:
            tg.send(chat_id, "Usage: /threshold 70  (between 40 and 100)")
        return

    if text.startswith("/pause"):
        sb.table("users").update({"active": False}).eq("id", user["id"]).execute()
        tg.send(chat_id, "⏸ Paused. /resume to restart.")
        return

    if text.startswith("/resume"):
        sb.table("users").update({"active": True}).eq("id", user["id"]).execute()
        tg.send(chat_id, "▶️ Resumed.")
        return

    if text:
        tg.send(chat_id, HELP)


def handle_updates() -> None:
    """FIX: the Telegram offset is now committed AFTER each update is fully handled.
    Phase 1 advanced it before processing, so any crash mid-loop threw the message away
    (this is exactly why the 'CV error then silence' incident needed a manual re-send)."""
    for u in tg.get_updates():
        try:
            if u.get("callback_query"):
                _handle_callback(u["callback_query"])
            elif u.get("message"):
                _handle_message(u["message"])
        except Exception as e:
            log_event("update_handler_error", {"update_id": u.get("update_id"),
                                               "error": str(e)[:400]})
        finally:
            tg.commit_offset(u["update_id"])


# ---------------------------- matching ----------------------------

def _prefilter(profile: dict, job: dict) -> bool:
    """Cheap keyword gate so the free LLM quota is spent only on plausible fits."""
    hay = f"{job['title']} {job.get('description','')}".lower()
    title_l = (job.get("title") or "").lower()
    skills = [s.lower() for s in profile.get("skills", []) if len(s or "") > 2]
    titles = [t.lower() for t in (profile.get("lateral_titles", [])
                                  + profile.get("next_step_titles", [])) if t]
    if any(t in title_l for t in titles):
        return True
    # Partial title overlap: "marketing analyst" vs "analyst - marketing operations"
    for t in titles:
        words = [w for w in t.split() if len(w) > 3]
        if words and all(w in title_l for w in words):
            return True
    return sum(1 for s in skills if s in hay) >= 3


def run_matching() -> None:
    users = (sb.table("users").select("*")
             .eq("active", True).not_.is_("profile", "null").execute().data) or []
    if not users:
        return

    total_budget = MAX_LLM_SCORES_PER_RUN
    for user in users:
        if total_budget <= 0:
            break
        profile, chat_id = user["profile"], user["telegram_chat_id"]

        # FIX: per-user cursor. Phase 1's single global cursor meant a user who joined
        # today never saw yesterday's jobs, and any job skipped when the budget ran out
        # was skipped forever.
        cursor = get_cursor(user["id"])
        if cursor < 0:
            top = sb.table("jobs").select("id").order("id", desc=True).limit(1).execute().data
            cursor = max(0, (int(top[0]["id"]) if top else 0) - NEW_USER_BACKFILL)

        jobs = (sb.table("jobs")
                .select("id,title,company,location,description,url,ats,source,apply_policy")
                .gt("id", cursor).order("id")
                .limit(MAX_JOBS_PER_USER_PER_RUN).execute().data) or []
        if not jobs:
            continue

        candidates = [j for j in jobs if _prefilter(profile, j)]
        if not candidates:
            # FIX: nothing in this page can ever match this profile, so clear the whole
            # page. Previously the cursor never advanced here, so once 400+ non-matching
            # jobs piled up the user was stuck re-scanning them forever and never
            # reached anything newer.
            set_cursor(user["id"], jobs[-1]["id"])
            continue

        user_batches = min(MAX_LLM_BATCHES_PER_USER, total_budget)
        sent, batches_used = 0, 0
        cleared_upto = cursor      # highest candidate id whose batch scored successfully
        stopped_early = False      # budget / message cap / error ended the page early
        assisted = user.get("apply_mode") == "assisted"

        # Location prefs make the scorer cap jobs in cities you'd never move to.
        answers = ap.get_answers(user["id"])
        prefs = {"home_locations": profile.get("locations"),
                 "current_location": answers.get("current_location"),
                 "willing_to_relocate": answers.get("willing_to_relocate")}
        # Everything surfaced in the last 45 days, for the cross-source duplicate guard.
        surfaced = ap.recent_match_pairs(user["id"])

        for i in range(0, len(candidates), 8):
            if batches_used >= user_batches or sent >= MAX_MATCH_MESSAGES_PER_USER:
                stopped_early = True
                break
            batch = candidates[i:i + 8]
            batches_used += 1
            total_budget -= 1
            try:
                results = score_jobs(profile, batch, prefs)
            except Exception as e:
                log_event("score_error", {"user_id": user["id"], "error": str(e)[:300]})
                # FIX: stop here rather than skipping ahead — the cursor must never
                # pass an unscored batch, otherwise those jobs are lost forever.
                stopped_early = True
                break

            chat_dead = False
            for res in results:
                try:
                    job = batch[int(res["index"])]
                    score = int(res["score"])
                except Exception:
                    continue
                if score < user["threshold"] or sent >= MAX_MATCH_MESSAGES_PER_USER:
                    continue

                reason = ap.is_blocked(user, job)
                if reason:
                    log_event("match_suppressed", {"user_id": user["id"], "job": job["id"],
                                                   "reason": reason})
                    continue

                # Same role found by a second source (different id, so dedup_hash
                # let it in) — never notify twice.
                if ap.is_duplicate_surface(surfaced, job):
                    log_event("match_suppressed", {"user_id": user["id"], "job": job["id"],
                                                   "reason": "duplicate of an earlier match"})
                    continue

                try:
                    ins = sb.table("matches").insert({
                        "user_id": user["id"], "job_id": job["id"], "score": score,
                        "classification": res.get("classification", "lateral"),
                        "rationale": res.get("rationale", ""),
                    }).execute()
                    match_id = ins.data[0]["id"]
                    surfaced.append((ap._norm_key(job.get("title")),
                                     ap._norm_key(job.get("company"))))
                except Exception:
                    continue      # already surfaced before

                badge = ("⬆️ NEXT STEP" if res.get("classification") == "next_step"
                         else "↔️ Lateral")
                manual = job.get("apply_policy") == "manual_only"
                tag = ""
                if manual:
                    tag = f"\n📬 via {job.get('source', '').replace('email:', '')} alert " \
                          "— open in your own browser (never automated)"

                body = (f"🎯 {score}/100 · {badge}\n"
                        f"{job['title']} — {job.get('company', '')}\n"
                        f"📍 {job.get('location', '')}\n"
                        f"💡 {res.get('rationale', '')}{tag}")

                if assisted and not ap.cap_reached(user):
                    checklist, letter = ap.build_package(user, job, score,
                                                         res.get("rationale", ""))
                    ap.record_application(user["id"], match_id, job,
                                          method="assisted", cover=letter)
                    body += "\n\n" + checklist
                    if letter:
                        body += "\n\n✍️ Cover letter:\n" + letter
                elif assisted:
                    body += "\n\n🧢 Daily application cap reached — saved for tomorrow."

                if not tg.send(chat_id, body, buttons=_match_buttons(match_id, job["url"])):
                    chat_dead = True   # blocked/deleted; stop pushing to this chat
                    break
                sent += 1

            cleared_upto = max(cleared_upto, max(j["id"] for j in batch))
            if chat_dead:
                stopped_early = True
                break

        # Advance past everything actually dealt with: the whole page when every
        # candidate batch scored, otherwise only up to the last successful batch
        # (prefilter-rejected jobs below it are covered implicitly; anything above
        # it is retried next run).
        set_cursor(user["id"],
                   max(cursor, jobs[-1]["id"] if not stopped_early else cleared_upto))


def main() -> None:
    handle_updates()      # registrations, CV uploads, commands, button taps
    ingest.ingest_all()   # pull fresh jobs (APIs + open ATS + alert emails)
    run_matching()        # score for every active user & notify
    try:
        digest.run_daily_health()     # once a day: is everything actually working?
        digest.run_weekly_digest()    # Mondays: your numbers + tuning suggestions
    except Exception as e:
        log_event("digest_error", {"error": str(e)[:300]})
    handle_updates()      # catch anything sent during the run


if __name__ == "__main__":
    main()
