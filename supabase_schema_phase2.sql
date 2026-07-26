-- ============================================================
-- JobAutopilot — Phase 2 + Phase 3 schema additions
-- Run ONCE in Supabase → SQL Editor. Safe to re-run (all IF NOT EXISTS).
-- Does not touch or drop anything from Phase 1.
-- ============================================================

-- ---------- Phase 2: per-user answer bank + apply consent ----------

-- The answer bank holds the facts every application form asks for.
-- Answers come ONLY from here (never invented by the LLM) — this is the
-- honesty guard: nothing is ever submitted that the user did not write.
create table if not exists answer_bank (
  user_id      bigint primary key references users(id) on delete cascade,
  answers      jsonb not null default '{}'::jsonb,
  cv_file_id   text,          -- Telegram file_id of the user's CV PDF (re-downloadable)
  cv_file_name text,
  updated_at   timestamptz not null default now()
);

-- Apply-related per-user settings live on users so existing reads pick them up.
alter table users add column if not exists apply_mode text not null default 'off';
  -- 'off'      = notify only (Phase 1 behaviour, the default)
  -- 'assisted' = bot pre-builds the full package + one-tap apply button
  -- 'auto'     = Tier-2 local browser automation may fill the form (still
  --              requires the user to tap Confirm before anything is submitted)

alter table users add column if not exists daily_apply_cap int not null default 10;
alter table users add column if not exists blocklist jsonb not null default '[]'::jsonb;
  -- e.g. ["current employer pvt ltd", "some staffing agency"]
alter table users add column if not exists email_alerts_enabled boolean not null default true;

-- Extend the Phase 1 applications table into a real audit trail.
alter table applications add column if not exists user_id bigint references users(id);
alter table applications add column if not exists method text;
  -- 'assisted' | 'tier2_browser' | 'manual'
alter table applications add column if not exists cover_letter text;
alter table applications add column if not exists screenshot_note text;

-- ---------- Phase 3: email-sourced (manual-only) listings ----------

-- Jobs discovered from job-alert EMAILS (LinkedIn / Naukri / IIMJobs / Instahyre).
-- These are flagged manual_only forever: the system never posts to those sites.
alter table jobs add column if not exists apply_policy text not null default 'auto_ok';
  -- 'auto_ok'     = open ATS, Tier-2 automation may be attempted
  -- 'manual_only' = closed platform; one-tap link for the human, never automated

-- Remember which alert emails have been consumed so nothing is parsed twice.
create table if not exists email_seen (
  message_id text primary key,
  seen_at    timestamptz not null default now()
);

-- ---------- Reliability: per-user matching checkpoint ----------

-- Phase 1 used ONE global checkpoint in `state`, which meant (a) a user who
-- registered today never saw yesterday's jobs, and (b) jobs skipped because the
-- LLM budget ran out were skipped forever. One row per user fixes both.
create table if not exists user_cursor (
  user_id      bigint primary key references users(id) on delete cascade,
  last_job_id  bigint not null default 0,
  updated_at   timestamptz not null default now()
);

-- ---------- Indexes ----------

create index if not exists idx_jobs_policy      on jobs (apply_policy, id desc);
create index if not exists idx_matches_status   on matches (user_id, status, created_at desc);
create index if not exists idx_apps_user_time   on applications (user_id, created_at desc);
create index if not exists idx_events_kind_time on events (kind, created_at desc);

-- ---------- Row Level Security (match Phase 1 posture) ----------

alter table answer_bank  enable row level security;
alter table email_seen   enable row level security;
alter table user_cursor  enable row level security;
-- No policies created on purpose: with RLS on and no policy, the public/anon key
-- can read nothing. Only the service_role key (in GitHub Secrets) has access.
