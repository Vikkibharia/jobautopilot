import json
import time
import requests
from .config import GEMINI_API_KEY, GEMINI_MODEL

BASE = "https://generativelanguage.googleapis.com/v1beta"
HEADERS = {"x-goog-api-key": GEMINI_API_KEY}
_working_model = None


def _candidate_models() -> list[str]:
    try:
        r = requests.get(f"{BASE}/models?pageSize=200", headers=HEADERS, timeout=30)
        names = [m["name"].removeprefix("models/")
                 for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception:
        names = []

    def usable(n):
        bad = ("tts", "image", "audio", "live", "embedding", "veo", "imagen", "thinking")
        return not any(b in n for b in bad)

    lite = sorted([n for n in names if "flash-lite" in n and usable(n)], reverse=True)
    flash = sorted([n for n in names if "flash" in n and "lite" not in n and usable(n)], reverse=True)
    ordered = lite + flash
    return ordered[:6] or ["gemini-flash-lite-latest", "gemini-flash-latest", GEMINI_MODEL]


def _generate(prompt: str) -> str:
    global _working_model
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    models = _candidate_models()
    if _working_model in models:
        models.remove(_working_model)
    if _working_model:
        models.insert(0, _working_model)

    errors = []
    for model in models:
        for attempt in range(2):
            r = requests.post(f"{BASE}/models/{model}:generateContent",
                              json=body, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                _working_model = model
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if r.status_code == 429:
                time.sleep(20)
                continue
            break
        errors.append(f"{model}:{r.status_code}")
    raise RuntimeError(f"Gemini failed on all models: {', '.join(errors)}")


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
