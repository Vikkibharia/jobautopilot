"""OPTIONAL Tier-2: local browser form-filling. Run on YOUR machine, not in CI.

    pip install playwright && playwright install chromium
    python -m src.apply_tier2            # fill + screenshot, submits nothing
    python -m src.apply_tier2 --submit   # only after you've watched it work

Read this before using it
-------------------------
This fills the visible fields on an open-ATS application page and stops. It does
NOT click submit unless you pass --submit, and it refuses outright on any page with
a CAPTCHA or a login wall. That restraint is deliberate:

  * Most ATS and employer terms prohibit automated submission. Greenhouse/Lever have
    no candidate-side API, so this is browser automation, and it is on you.
  * Employers increasingly detect botted applications and discard them, so blasting
    volume lowers your hit rate. The cap in config.py exists for that reason.
  * A wrong notice period or CTC auto-filled into your dream company's form is worse
    than fifty applications you never sent. Anything not in your answer bank is left
    BLANK for you to fill, never guessed.

Honest assessment: for a single job seeker, the assisted flow in apply.py gets you to
"submitted" faster and more reliably than maintaining selectors for this. Use Tier-2
only if you are applying at genuine volume and will supervise it.
"""

import argparse
import sys

from .config import sb, log_event
from .apply import get_answers, cap_reached, is_blocked
from .llm import map_form_answers

FIELD_HINTS = {
    "full_name": ["full name", "your name", "name"],
    "email": ["email"],
    "phone": ["phone", "mobile", "contact number"],
    "current_location": ["location", "city", "where are you based"],
    "notice_period": ["notice period", "how soon can you join", "availability"],
    "current_ctc": ["current ctc", "current salary", "present salary"],
    "expected_ctc": ["expected ctc", "expected salary", "salary expectation"],
    "total_experience": ["years of experience", "total experience", "experience"],
    "linkedin": ["linkedin"],
    "portfolio": ["website", "portfolio", "github"],
    "work_authorization": ["authorized", "authorisation", "visa", "sponsorship", "citizen"],
}

BLOCKERS = ["recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile", "cloudflare",
            "sign in to apply", "log in to apply"]


def _pick(label: str, answers: dict) -> str | None:
    low = label.lower()
    for key, hints in FIELD_HINTS.items():
        if any(h in low for h in hints) and answers.get(key):
            return answers[key]
    return None


def fill_one(page, url: str, answers: dict, cv_path: str | None, submit: bool) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    body = (page.content() or "").lower()
    for b in BLOCKERS:
        if b in body:
            return f"demoted_to_manual: page has {b}"

    filled, skipped = [], []
    for inp in page.query_selector_all("input:not([type=hidden]), textarea, select"):
        try:
            if not inp.is_visible() or not inp.is_editable():
                continue
            label = " ".join(filter(None, [
                inp.get_attribute("aria-label"), inp.get_attribute("placeholder"),
                inp.get_attribute("name"), inp.get_attribute("id")]))
            itype = (inp.get_attribute("type") or "").lower()
            if itype == "file":
                if cv_path:
                    inp.set_input_files(cv_path)
                    filled.append("cv")
                continue
            val = _pick(label, answers)
            if val:
                inp.fill(val)
                filled.append(label[:40])
            elif inp.get_attribute("required") is not None:
                skipped.append(label[:60])      # required but unknown -> human must finish
        except Exception:
            continue

    shot = f"apply_{abs(hash(url)) % 10**8}.png"
    page.screenshot(path=shot, full_page=True)

    if skipped:
        return f"demoted_to_manual: {len(skipped)} required field(s) not in answer bank " \
               f"({'; '.join(skipped[:3])}) — screenshot {shot}"
    if not submit:
        return f"filled_not_submitted: {len(filled)} fields — review {shot}, then use --submit"

    for sel in ["button[type=submit]", "input[type=submit]", "text=/^submit application$/i"]:
        btn = page.query_selector(sel)
        if btn:
            btn.click()
            page.wait_for_timeout(4000)
            page.screenshot(path=f"submitted_{shot}")
            return f"submitted — proof in submitted_{shot}"
    return f"demoted_to_manual: no submit button found — screenshot {shot}"


def main() -> None:
    argp = argparse.ArgumentParser()
    argp.add_argument("--submit", action="store_true", help="actually click submit")
    argp.add_argument("--limit", type=int, default=3)
    argp.add_argument("--cv", default=None, help="path to your CV PDF")
    args = argp.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")

    users = sb.table("users").select("*").eq("apply_mode", "auto").execute().data or []
    for user in users:
        if cap_reached(user):
            print(f"user {user['id']}: daily cap reached")
            continue
        answers = get_answers(user["id"])
        rows = (sb.table("matches")
                .select("id, job_id, jobs(url, title, company, ats, apply_policy)")
                .eq("user_id", user["id"]).eq("status", "notified")
                .order("score", desc=True).limit(args.limit * 3).execute().data) or []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)   # visible on purpose: watch it
            page = browser.new_page()
            done = 0
            for row in rows:
                job = row.get("jobs") or {}
                if job.get("apply_policy") != "auto_ok" or job.get("ats") not in ("greenhouse", "lever"):
                    continue                              # never automate closed platforms
                if is_blocked(user, job) or done >= args.limit:
                    continue
                result = fill_one(page, job["url"], answers, args.cv, args.submit)
                print(f"[{job.get('company')}] {job.get('title')}: {result}")
                log_event("tier2_attempt", {"user_id": user["id"], "match": row["id"],
                                            "result": result[:300]})
                done += 1
            browser.close()


if __name__ == "__main__":
    main()
