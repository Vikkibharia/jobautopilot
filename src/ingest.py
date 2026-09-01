import hashlib
import html
import json
import re
from email.utils import parsedate_to_datetime

import requests
from .config import (sb, ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_API_KEY,
                     CAREERJET_AFFID, log_event)
from .email_ingest import fetch_email_alerts

HEADERS = {"User-Agent": "JobAutopilot/2.0"}
INDIA_KEYS = ("india", "remote", "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon",
              "gurugram", "hyderabad", "pune", "chennai", "noida", "kolkata", "ahmedabad",
              "jaipur", "indore", "kochi", "coimbatore", "chandigarh", "anywhere")


def _clean(text: str) -> str:
    """FIX: Phase 1 stripped tags but left HTML entities, so Greenhouse descriptions
    reached Gemini full of &amp;#39; and &nbsp; — noise the scorer had to pay tokens for."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _dedup_hash(row: dict) -> str:
    """FIX: Phase 1 hashed title|company|location only, so two genuinely different
    'Software Engineer' roles at the same company in Bengaluru collapsed into one.
    Where a source gives a stable id, use it."""
    ext = (row.get("external_id") or "").strip()
    if ext:
        key = f"{row.get('source','')}|{ext}"
    else:
        key = (f"{(row.get('title') or '').lower().strip()}|"
               f"{(row.get('company') or '').lower().strip()}|"
               f"{(row.get('location') or '').lower().strip()[:20]}")
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _save(rows: list[dict]) -> int:
    """FIX: Phase 1 did one HTTP INSERT per job (up to ~400 round trips per run and a
    swallowed exception each time a duplicate hit the UNIQUE constraint). One upsert
    with ignore_duplicates does the same job in a couple of calls."""
    if not rows:
        return 0
    clean = []
    seen = set()
    for row in rows:
        if not (row.get("title") and row.get("url")):
            continue
        row["dedup_hash"] = _dedup_hash(row)
        if row["dedup_hash"] in seen:          # dedupe within this batch too
            continue
        seen.add(row["dedup_hash"])
        row.setdefault("apply_policy", "auto_ok")
        clean.append(row)

    saved = 0
    for i in range(0, len(clean), 100):
        chunk = clean[i:i + 100]
        try:
            r = (sb.table("jobs")
                 .upsert(chunk, on_conflict="dedup_hash", ignore_duplicates=True)
                 .execute())
            saved += len(r.data or [])
        except Exception as e:
            log_event("save_error", {"error": str(e)[:300], "batch": len(chunk)})
    return saved


# ---------------------------- keyword-driven sources ----------------------------

def _user_search_terms(limit: int = 8) -> list[str]:
    """FIX (biggest match-quality lever): Phase 1 asked Adzuna for the newest 50 India
    jobs of ANY kind, then hoped the prefilter found something. With ~800k live jobs in
    India, a blind newest-50 feed almost never contains a role for a specific person.
    Now we ask Adzuna for the titles our users actually want."""
    try:
        rows = (sb.table("users").select("profile")
                .eq("active", True).not_.is_("profile", "null").execute().data) or []
    except Exception:
        return []
    terms, seen = [], set()
    for row in rows:
        p = row.get("profile") or {}
        for t in ((p.get("lateral_titles") or [])[:4] + (p.get("next_step_titles") or [])[:3]):
            t = (t or "").strip().lower()
            if t and t not in seen and 3 < len(t) < 60:
                seen.add(t)
                terms.append(t)
    return terms[:limit]


def fetch_adzuna() -> list[dict]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    out = []
    terms = _user_search_terms() or [""]        # "" keeps Phase 1's generic sweep as a floor
    for term in terms:
        url = ("https://api.adzuna.com/v1/api/jobs/in/search/1"
               f"?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}"
               "&results_per_page=50&max_days_old=2&sort_by=date"
               "&content-type=application/json")
        if term:
            url += f"&what={requests.utils.quote(term)}"
        try:
            data = requests.get(url, headers=HEADERS, timeout=30).json()
        except Exception as e:
            log_event("ingest_error", {"source": "adzuna", "term": term, "error": str(e)[:300]})
            continue
        for j in data.get("results", []):
            if not (j.get("title") and j.get("redirect_url")):
                continue
            out.append({
                "source": "adzuna",
                "external_id": str(j.get("id", "")),
                "title": j.get("title", "")[:300],
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "description": _clean(j.get("description", ""))[:6000],
                "url": j.get("redirect_url", ""),
                "ats": "unknown",
                "salary": str(j.get("salary_min") or ""),
                "posted_at": j.get("created"),
            })
    return out


def fetch_jooble() -> list[dict]:
    if not JOOBLE_API_KEY:
        return []
    out = []
    # FIX: Phase 1 posted keywords:"" which Jooble often answers with an error or noise.
    for term in (_user_search_terms(4) or ["analyst"]):
        try:
            r = requests.post(f"https://jooble.org/api/{JOOBLE_API_KEY}",
                              json={"keywords": term, "location": "India", "page": "1"},
                              headers=HEADERS, timeout=30)
            data = r.json()
        except Exception as e:
            log_event("ingest_error", {"source": "jooble", "term": term, "error": str(e)[:300]})
            continue
        for j in data.get("jobs", []):
            if not (j.get("title") and j.get("link")):
                continue
            out.append({
                "source": "jooble",
                "external_id": str(j.get("id", "")),
                "title": j.get("title", "")[:300],
                "company": j.get("company", ""),
                "location": j.get("location", ""),
                "description": _clean(j.get("snippet", ""))[:6000],
                "url": j.get("link", ""),
                "ats": "unknown",
                "salary": j.get("salary", ""),
                "posted_at": None,
            })
    return out


def fetch_remotive() -> list[dict]:
    try:
        data = requests.get("https://remotive.com/api/remote-jobs?limit=100",
                            headers=HEADERS, timeout=30).json()
    except Exception as e:
        log_event("ingest_error", {"source": "remotive", "error": str(e)[:300]})
        return []
    out = []
    for j in data.get("jobs", []):
        region = (j.get("candidate_required_location") or "").lower()
        if region and not any(k in region for k in ("india", "worldwide", "anywhere", "asia")):
            continue
        out.append({
            "source": "remotive",
            "external_id": str(j.get("id", "")),
            "title": j.get("title", "")[:300],
            "company": j.get("company_name", ""),
            "location": f"Remote ({j.get('candidate_required_location') or 'anywhere'})",
            "description": _clean(j.get("description", ""))[:6000],
            "url": j.get("url", ""),
            "ats": "unknown",
            "salary": j.get("salary", ""),
            "posted_at": j.get("publication_date"),
        })
    return out


def _rfc_date(s: str) -> str | None:
    """Careerjet dates arrive like 'Wed,15 Nov 2025 19:13:43 GMT' — convert to ISO
    so a single odd date can never make the database reject a whole batch."""
    try:
        return parsedate_to_datetime(s).isoformat()
    except Exception:
        return None


def fetch_careerjet() -> list[dict]:
    """Adzuna alternative: Careerjet's Search API v4 has strong India coverage and
    takes keyword queries. The key from the publisher dashboard is used as the
    basic-auth username with an empty password (per their v4 docs). Note: the old
    public.api.careerjet.net endpoint is closed to new accounts — don't go back."""
    if not CAREERJET_AFFID:
        return []
    out = []
    for term in (_user_search_terms() or ["analyst"]):
        try:
            r = requests.get(
                "https://search.api.careerjet.net/v4/query",
                params={"locale_code": "en_IN", "keywords": term, "location": "India",
                        "sort": "date", "page_size": "50", "fragment_size": "1000",
                        "user_ip": "1.2.3.4", "user_agent": HEADERS["User-Agent"]},
                auth=(CAREERJET_AFFID, ""), headers=HEADERS, timeout=30)
            data = r.json()
        except Exception as e:
            log_event("ingest_error", {"source": "careerjet", "term": term,
                                       "error": str(e)[:300]})
            continue
        # Careerjet reports problems (bad key, unknown location, throttling) inside
        # the response body. Surface them instead of counting them as "0 jobs".
        if data.get("type") != "JOBS":
            log_event("ingest_error", {"source": "careerjet", "term": term,
                                       "error": (str(data.get("error") or data.get("message")
                                                     or data) + f" [http {r.status_code}]")[:250]})
            continue
        for j in data.get("jobs", []):
            if not (j.get("title") and j.get("url")):
                continue
            out.append({
                "source": "careerjet",
                "external_id": "",
                "title": _clean(j.get("title", ""))[:300],
                "company": j.get("company", ""),
                "location": j.get("locations", "India"),
                "description": _clean(j.get("description", ""))[:6000],
                "url": j.get("url", ""),
                "ats": "unknown",
                "salary": j.get("salary", ""),
                "posted_at": _rfc_date(j.get("date", "")),
            })
    return out


def fetch_themuse() -> list[dict]:
    """The Muse public API: keyless, lists roles at larger companies with real India
    offices (many GCCs). Location-filtered server-side."""
    locations = ["Bengaluru, India", "Mumbai, India", "Delhi, India", "Gurgaon, India",
                 "Hyderabad, India", "Pune, India", "Chennai, India", "Noida, India"]
    out = []
    for page in (0, 1):
        try:
            data = requests.get("https://www.themuse.com/api/public/jobs",
                                params=[("page", page)] + [("location", l) for l in locations],
                                headers=HEADERS, timeout=30).json()
        except Exception as e:
            log_event("ingest_error", {"source": "themuse", "error": str(e)[:300]})
            break
        results = data.get("results") or []
        if not results:
            break
        for j in results:
            url = (j.get("refs") or {}).get("landing_page", "")
            if not (j.get("name") and url):
                continue
            locs = ", ".join(l.get("name", "") for l in (j.get("locations") or []))
            out.append({
                "source": "themuse",
                "external_id": str(j.get("id", "")),
                "title": j.get("name", "")[:300],
                "company": (j.get("company") or {}).get("name", ""),
                "location": locs[:300] or "India",
                "description": _clean(j.get("contents", ""))[:6000],
                "url": url,
                "ats": "unknown",
                "salary": "",
                "posted_at": j.get("publication_date"),
            })
    return out


def fetch_remote_boards() -> list[dict]:
    """Keyless remote-job APIs that accept India-based candidates:
    RemoteOK + Jobicy (Remotive already runs separately)."""
    out = []
    try:
        data = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=30).json()
        for j in (data[1:] if isinstance(data, list) else []):   # [0] is a legal notice
            loc = (j.get("location") or "").lower()
            if loc and not any(k in loc for k in ("india", "worldwide", "anywhere", "asia", "remote")):
                continue
            if not (j.get("position") and j.get("url")):
                continue
            out.append({
                "source": "remoteok", "external_id": str(j.get("id", "")),
                "title": j.get("position", "")[:300], "company": j.get("company", ""),
                "location": f"Remote ({j.get('location') or 'anywhere'})",
                "description": _clean(j.get("description", ""))[:6000],
                "url": j.get("url", ""), "ats": "unknown",
                "salary": "", "posted_at": j.get("date"),
            })
    except Exception as e:
        log_event("ingest_error", {"source": "remoteok", "error": str(e)[:300]})

    try:
        data = requests.get("https://jobicy.com/api/v2/remote-jobs?count=50",
                            headers=HEADERS, timeout=30).json()
        for j in data.get("jobs", []):
            geo = (j.get("jobGeo") or "").lower()
            if geo and not any(k in geo for k in ("india", "anywhere", "apac", "asia")):
                continue
            if not (j.get("jobTitle") and j.get("url")):
                continue
            out.append({
                "source": "jobicy", "external_id": str(j.get("id", "")),
                "title": j.get("jobTitle", "")[:300], "company": j.get("companyName", ""),
                "location": f"Remote ({j.get('jobGeo') or 'anywhere'})",
                "description": _clean(j.get("jobDescription") or j.get("jobExcerpt") or "")[:6000],
                "url": j.get("url", ""), "ats": "unknown",
                "salary": "", "posted_at": j.get("pubDate"),
            })
    except Exception as e:
        log_event("ingest_error", {"source": "jobicy", "error": str(e)[:300]})
    return out


# ---------------------------- open ATS boards ----------------------------

def _india_ok(loc: str) -> bool:
    return (not loc) or any(k in loc.lower() for k in INDIA_KEYS)


def fetch_ats_boards() -> list[dict]:
    try:
        with open("seed_companies.json") as f:
            seeds = json.load(f)
    except Exception as e:
        log_event("ingest_error", {"source": "seed_file", "error": str(e)[:200]})
        return []

    out = []
    for company in seeds.get("greenhouse", []):
        try:
            data = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true",
                headers=HEADERS, timeout=20).json()
            for j in data.get("jobs", []):
                loc = ((j.get("location") or {}).get("name") or "")
                if not _india_ok(loc):
                    continue
                out.append({
                    "source": "greenhouse", "external_id": str(j.get("id", "")),
                    "title": (j.get("title") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("content", ""))[:6000],
                    "url": j.get("absolute_url", ""), "ats": "greenhouse",
                    "apply_policy": "auto_ok",
                    "salary": "", "posted_at": j.get("first_published") or j.get("updated_at"),
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"greenhouse:{company}", "error": str(e)[:300]})

    for company in seeds.get("lever", []):
        try:
            data = requests.get(f"https://api.lever.co/v0/postings/{company}?mode=json",
                                headers=HEADERS, timeout=20).json()
            for j in (data if isinstance(data, list) else []):
                loc = ((j.get("categories") or {}).get("location") or "")
                if not _india_ok(loc):
                    continue
                out.append({
                    "source": "lever", "external_id": str(j.get("id", "")),
                    "title": (j.get("text") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("descriptionPlain", ""))[:6000],
                    "url": j.get("hostedUrl", ""), "ats": "lever",
                    "apply_policy": "auto_ok",
                    "salary": "", "posted_at": None,
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"lever:{company}", "error": str(e)[:300]})

    # ---- Ashby (jobs.ashbyhq.com/<slug>) — public JSON, no key ----
    for company in seeds.get("ashby", []):
        try:
            data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{company}",
                                headers=HEADERS, timeout=20).json()
            for j in data.get("jobs", []):
                loc = j.get("location") or ""
                if not (_india_ok(loc) or j.get("isRemote")):
                    continue
                out.append({
                    "source": "ashby", "external_id": str(j.get("id", "")),
                    "title": (j.get("title") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("descriptionHtml", ""))[:6000],
                    "url": j.get("jobUrl") or j.get("applyUrl", ""), "ats": "ashby",
                    "apply_policy": "auto_ok", "salary": "",
                    "posted_at": j.get("publishedAt"),
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"ashby:{company}", "error": str(e)[:300]})

    # ---- SmartRecruiters (careers.smartrecruiters.com/<Company>) — public, no key ----
    for company in seeds.get("smartrecruiters", []):
        try:
            data = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                "?limit=100&country=in",       # server-side India filter (verified live)
                headers=HEADERS, timeout=20).json()
            for j in data.get("content", []):
                loc = j.get("location") or {}
                city, country = loc.get("city", ""), (loc.get("country") or "").lower()
                if country and country != "in" and not _india_ok(city):
                    continue
                out.append({
                    "source": "smartrecruiters", "external_id": str(j.get("id", "")),
                    "title": (j.get("name") or "")[:300], "company": company,
                    "location": ", ".join(x for x in (city, loc.get("region", "")) if x) or "India",
                    "description": "",           # postings list carries no JD; scorer knows
                    "url": f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                    "ats": "smartrecruiters", "apply_policy": "auto_ok",
                    "salary": "", "posted_at": j.get("releasedDate"),
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"smartrecruiters:{company}",
                                       "error": str(e)[:300]})

    # ---- Workable (apply.workable.com/<account>) — public widget API, no key ----
    for company in seeds.get("workable", []):
        try:
            data = requests.get(
                f"https://apply.workable.com/api/v1/widget/accounts/{company}?details=true",
                headers=HEADERS, timeout=20).json()
            for j in data.get("jobs", []):
                loc = ", ".join(x for x in (j.get("city", ""), j.get("country", "")) if x)
                if not _india_ok(loc):
                    continue
                out.append({
                    "source": "workable", "external_id": str(j.get("shortcode", "")),
                    "title": (j.get("title") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("description", ""))[:6000],
                    "url": j.get("url", ""), "ats": "workable",
                    "apply_policy": "auto_ok", "salary": "", "posted_at": None,
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"workable:{company}", "error": str(e)[:300]})

    # ---- Recruitee (<company>.recruitee.com) — public JSON, no key ----
    for company in seeds.get("recruitee", []):
        try:
            data = requests.get(f"https://{company}.recruitee.com/api/offers/",
                                headers=HEADERS, timeout=20).json()
            for j in data.get("offers", []):
                loc = ", ".join(x for x in (j.get("city", ""), j.get("country", "")) if x)
                if not _india_ok(loc):
                    continue
                out.append({
                    "source": "recruitee", "external_id": str(j.get("id", "")),
                    "title": (j.get("title") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("description", ""))[:6000],
                    "url": j.get("careers_url", ""), "ats": "recruitee",
                    "apply_policy": "auto_ok", "salary": "", "posted_at": None,
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"recruitee:{company}", "error": str(e)[:300]})
    return out


def ingest_all() -> int:
    total, per_source = 0, {}
    for fetcher in (fetch_adzuna, fetch_careerjet, fetch_jooble, fetch_themuse,
                    fetch_remotive, fetch_remote_boards, fetch_ats_boards,
                    fetch_email_alerts):
        try:
            rows = fetcher()
        except Exception as e:
            log_event("ingest_error", {"source": fetcher.__name__, "error": str(e)[:300]})
            continue
        n = _save(rows)
        per_source[fetcher.__name__] = n
        total += n
    log_event("ingest_done", {"new_jobs": total, "by_source": per_source})
    return total
