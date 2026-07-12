import hashlib
import json
import re
import requests
from .config import sb, ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_API_KEY, log_event

HEADERS = {"User-Agent": "JobAutopilot/1.0"}


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _dedup_hash(title: str, company: str, location: str) -> str:
    key = f"{(title or '').lower().strip()}|{(company or '').lower().strip()}|{(location or '').lower().strip()[:20]}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _save(rows: list[dict]) -> int:
    saved = 0
    for row in rows:
        row["dedup_hash"] = _dedup_hash(row["title"], row.get("company", ""), row.get("location", ""))
        try:
            sb.table("jobs").insert(row).execute()
            saved += 1
        except Exception:
            pass  # duplicate hash -> already known
    return saved


def fetch_adzuna() -> list[dict]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    url = (
        "https://api.adzuna.com/v1/api/jobs/in/search/1"
        f"?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}"
        "&results_per_page=50&max_days_old=1&sort_by=date&content-type=application/json"
    )
    try:
        data = requests.get(url, headers=HEADERS, timeout=30).json()
    except Exception as e:
        log_event("ingest_error", {"source": "adzuna", "error": str(e)})
        return []
    return [
        {
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
        }
        for j in data.get("results", [])
        if j.get("title") and j.get("redirect_url")
    ]


def fetch_jooble() -> list[dict]:
    if not JOOBLE_API_KEY:
        return []
    try:
        r = requests.post(
            f"https://jooble.org/api/{JOOBLE_API_KEY}",
            json={"keywords": "", "location": "India", "datecreatedfrom": "", "page": "1"},
            headers=HEADERS, timeout=30,
        )
        data = r.json()
    except Exception as e:
        log_event("ingest_error", {"source": "jooble", "error": str(e)})
        return []
    return [
        {
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
        }
        for j in data.get("jobs", [])
        if j.get("title") and j.get("link")
    ]


def fetch_remotive() -> list[dict]:
    try:
        data = requests.get("https://remotive.com/api/remote-jobs?limit=100", headers=HEADERS, timeout=30).json()
    except Exception as e:
        log_event("ingest_error", {"source": "remotive", "error": str(e)})
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


def fetch_ats_boards() -> list[dict]:
    with open("seed_companies.json") as f:
        seeds = json.load(f)
    out = []
    for company in seeds.get("greenhouse", []):
        try:
            data = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true",
                headers=HEADERS, timeout=20,
            ).json()
            for j in data.get("jobs", []):
                loc = ((j.get("location") or {}).get("name") or "")
                if loc and not any(k in loc.lower() for k in ("india", "remote", "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon", "gurugram", "hyderabad", "pune", "chennai", "noida", "kolkata")):
                    continue
                out.append({
                    "source": "greenhouse", "external_id": str(j.get("id", "")),
                    "title": (j.get("title") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("content", ""))[:6000],
                    "url": j.get("absolute_url", ""), "ats": "greenhouse",
                    "salary": "", "posted_at": j.get("updated_at"),
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"greenhouse:{company}", "error": str(e)})
    for company in seeds.get("lever", []):
        try:
            data = requests.get(f"https://api.lever.co/v0/postings/{company}?mode=json", headers=HEADERS, timeout=20).json()
            for j in data if isinstance(data, list) else []:
                loc = ((j.get("categories") or {}).get("location") or "")
                if loc and not any(k in loc.lower() for k in ("india", "remote", "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon", "gurugram", "hyderabad", "pune", "chennai", "noida", "kolkata")):
                    continue
                out.append({
                    "source": "lever", "external_id": j.get("id", ""),
                    "title": (j.get("text") or "")[:300], "company": company,
                    "location": loc, "description": _clean(j.get("descriptionPlain", ""))[:6000],
                    "url": j.get("hostedUrl", ""), "ats": "lever",
                    "salary": "", "posted_at": None,
                })
        except Exception as e:
            log_event("ingest_error", {"source": f"lever:{company}", "error": str(e)})
    return out


def ingest_all() -> int:
    total = 0
    for fetcher in (fetch_adzuna, fetch_jooble, fetch_remotive, fetch_ats_boards):
        rows = fetcher()
        total += _save(rows)
    log_event("ingest_done", {"new_jobs": total})
    return total
