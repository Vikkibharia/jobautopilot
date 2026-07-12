import json
import time
import requests
from .config import GEMINI_API_KEY, GEMINI_MODEL

FALLBACK_MODELS = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-2.5-flash-lite"]


def _generate(prompt: str) -> str:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    last_error = "unknown"
    for model in FALLBACK_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, json=body, headers=headers, timeout=60)
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            last_error = f"model {model} returned status {r.status_code}"
            break
        else:
            last_error = f"model {model} rate limited"
    raise RuntimeError(f"Gemini request failed: {last_error}")


def _json(prompt: str):
    text = _generate(prompt).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)

def parse_cv(cv_text: str) -> dict:
    prompt = f"""You are a CV parser. Read the CV below and return ONLY a JSON object with:
  name (string),
  current_title (string),
  years_experience (number),
  level (one of: intern, junior, mid, senior, lead, staff, manager, director, executive),
  skills (array of up to 25 short skill strings, lowercase),
  lateral_titles (array of 5-10 job titles equivalent to the person's current level),
  next_step_titles (array of 5-10 job titles that are the natural next career step up),
  locations (array of Indian cities mentioned or implied; include "remote" if plausible; if none, use ["india"]),
  summary (one sentence).
Do not invent qualifications not present in the CV.

CV:
{cv_text[:15000]}"""
    return _json(prompt)


def score_jobs(profile: dict, jobs: list[dict]) -> list[dict]:
    """Score a small batch of jobs (<=8) against one profile. Returns list of
    {index, score, classification, rationale}."""
    jobs_block = "\n\n".join(
        f"[{i}] TITLE: {j['title']} | COMPANY: {j.get('company','?')} | LOCATION: {j.get('location','?')}\n"
        f"DESCRIPTION: {(j.get('description') or '')[:1500]}"
        for i, j in enumerate(jobs)
    )
    prompt = f"""You are a strict job-match scorer for a candidate in India.

CANDIDATE PROFILE:
{json.dumps({k: profile.get(k) for k in ('current_title','years_experience','level','skills','lateral_titles','next_step_titles','locations','summary')}, ensure_ascii=False)}

JOBS:
{jobs_block}

For EACH job return an object: index (int), score (0-100 integer, where 75+ means the candidate
should genuinely apply), classification ("lateral" if same level, "next_step" if one level up,
"stretch" if further), rationale (ONE short sentence naming the key overlap or gap).
Score 0-40 for wrong field, wrong country, or seniority far off.
Return ONLY a JSON array of these objects."""
    out = _json(prompt)
    return out if isinstance(out, list) else []
