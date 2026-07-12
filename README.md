# JobAutopilot — Free, Multi-User Job Scanner & Matcher (Phase 1)

A ₹0/month system where **anyone can upload their CV to your Telegram bot** and automatically
receive freshly-posted, PAN-India jobs matched to their profile every ~30 minutes —
with match scores, reasoning, and apply links. (Phase 2 adds auto-submission on open ATSs.)

**How users experience it:** they open your bot in Telegram → send `/start` → upload their CV
as a PDF → done. From then on, matching jobs land in their chat as they're found.

---

## Setup — step by step (about 30 minutes, all free, no coding)

### Step 1 — Create a GitHub account and repository
1. Sign up at https://github.com (free).
2. Click **New repository** → name it `jobautopilot` → set it to **Public**
   (public repos get **unlimited** free Actions minutes; your secret keys are NOT
   stored in the code — they go in encrypted Settings, Step 6).
3. Upload all the files from this folder to the repository
   (**Add file → Upload files**, drag the whole folder contents in, keep the folder
   structure — especially `.github/workflows/scan.yml`).

### Step 2 — Create your Telegram bot (2 minutes)
1. In Telegram, open **@BotFather** → send `/newbot`.
2. Give it a name (e.g., *JobAutopilot*) and a username (e.g., `MyJobAutopilotBot`).
3. BotFather replies with a **bot token** like `7213456789:AAH8x...` → copy it.
   This token is `TELEGRAM_BOT_TOKEN`.

### Step 3 — Create the free Supabase database
1. Sign up at https://supabase.com (free) → **New project** (choose the free plan,
   region: Mumbai/ap-south-1 if offered).
2. Once created, go to **SQL Editor** → paste the entire contents of
   `supabase_schema.sql` from this repo → **Run**. All tables are created.
3. Go to **Project Settings → API** and copy:
   - **Project URL** → this is `SUPABASE_URL`
   - **service_role key** (under "Project API keys") → this is `SUPABASE_SERVICE_KEY`

### Step 4 — Get your free Gemini API key
1. Go to https://aistudio.google.com → sign in with any Google account.
2. Click **Get API key** → **Create API key** → copy it.
   This is `GEMINI_API_KEY`. The free tier covers hundreds of scored jobs per day.

### Step 5 — Get free job-source API keys (optional but recommended)
1. **Adzuna** (best free India coverage): https://developer.adzuna.com → register →
   copy your **App ID** (`ADZUNA_APP_ID`) and **App Key** (`ADZUNA_APP_KEY`).
2. **Jooble**: https://jooble.org/api/about → request a free key (`JOOBLE_API_KEY`).
3. Skip either one and the system simply uses the remaining sources
   (Remotive + the Greenhouse/Lever company list work with no key at all).

### Step 6 — Add the keys to GitHub (encrypted secrets)
In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these, one by one (name exactly as shown):

| Secret name | Value from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Step 2 |
| `SUPABASE_URL` | Step 3 |
| `SUPABASE_SERVICE_KEY` | Step 3 |
| `GEMINI_API_KEY` | Step 4 |
| `ADZUNA_APP_ID` | Step 5 (optional) |
| `ADZUNA_APP_KEY` | Step 5 (optional) |
| `JOOBLE_API_KEY` | Step 5 (optional) |

### Step 7 — Switch it on
1. In your repo, open the **Actions** tab → enable workflows if prompted.
2. Click the **Job Scan** workflow → **Run workflow** to trigger the first run manually.
   After that it runs itself every 30 minutes automatically.
3. Open your bot in Telegram, send `/start`, then send your CV as a **PDF file**.
   Within one run cycle you'll get back a summary of your parsed profile, and matching
   jobs will start arriving.

### Step 8 — Invite users
Share the bot's Telegram link (`t.me/YourBotUsername`). Each person does `/start` + uploads
their PDF CV. Everyone gets their own matches in their own chat. On free tiers this
comfortably supports roughly 5–20 active users.

---

## Bot commands (for every user)

`/start` register · send a **PDF** to set/replace your CV · `/status` your profile summary
· `/threshold 70` change your match cutoff (default 75) · `/pause` and `/resume` matching
· `/help` list commands

## What Phase 1 does and doesn't do

Does: scan Adzuna, Jooble, Remotive and a growing Greenhouse/Lever company list every
30 minutes for India-relevant roles, deduplicate, pre-filter cheaply, score each survivor
against each user's CV with Gemini (0–100 + lateral/next-step classification + rationale),
and message each user their matches with apply links.

Doesn't yet: auto-submit applications. That's Phase 2 (direct Greenhouse/Lever/Ashby
submission with per-user answer banks, daily caps, audit log). The matching data model
already includes everything Phase 2 needs.

## Costs and limits

Everything runs on permanent free tiers: GitHub Actions (unlimited minutes on public
repos), Supabase (500 MB), Gemini free quota, Telegram (free). The practical ceilings are
Gemini's free daily quota (~hundreds of scored jobs/day across all users) and a 30-minute
scan cadence. Naukri/LinkedIn/Indeed are not scanned or auto-applied — automating them
violates their terms and gets user accounts banned; Phase 3 adds them as one-tap manual
notifications via parsed email alerts instead.
