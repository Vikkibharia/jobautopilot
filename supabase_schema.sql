-- JobAutopilot schema (Phase 1) — run this once in Supabase SQL Editor

create table if not exists users (
  id bigint generated always as identity primary key,
  telegram_chat_id bigint unique not null,
  name text,
  profile jsonb,                -- structured CV: skills, titles, level, locations, bands
  threshold int not null default 75,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists jobs (
  id bigint generated always as identity primary key,
  source text not null,          -- adzuna | jooble | remotive | greenhouse | lever
  external_id text,
  title text not null,
  company text,
  location text,
  description text,
  url text not null,
  ats text,                      -- greenhouse | lever | unknown
  salary text,
  posted_at timestamptz,
  dedup_hash text unique not null,
  created_at timestamptz not null default now()
);

create table if not exists matches (
  id bigint generated always as identity primary key,
  user_id bigint not null references users(id),
  job_id bigint not null references jobs(id),
  score int not null,
  classification text,           -- lateral | next_step | stretch
  rationale text,
  status text not null default 'notified',  -- notified | applied | skipped
  created_at timestamptz not null default now(),
  unique (user_id, job_id)
);

-- Phase 2 (auto-apply) will use this; created now so the model is complete
create table if not exists applications (
  id bigint generated always as identity primary key,
  match_id bigint not null references matches(id),
  tier int,
  payload jsonb,
  submitted_at timestamptz,
  outcome text,
  created_at timestamptz not null default now()
);

create table if not exists events (
  id bigint generated always as identity primary key,
  kind text not null,
  detail jsonb,
  created_at timestamptz not null default now()
);

-- key/value state: telegram update offset, per-source checkpoints
create table if not exists state (
  key text primary key,
  value text
);

create index if not exists idx_jobs_created on jobs (created_at desc);
create index if not exists idx_matches_user on matches (user_id, created_at desc);
