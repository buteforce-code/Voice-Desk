-- Voice Desk — initial schema (G3)
--
-- Invariants this schema enforces structurally, not by convention:
--   1. clinic_id on EVERY row. Multi-tenancy is not a retrofit.
--   2. No clinical columns anywhere. No diagnosis, symptom, result,
--      prescription or note. If a feature needs one, stop and escalate.
--   3. Call state is stored, never inferred.
--   4. Audit tables are append-only. The agent role has no DELETE grant.
--   5. Appointments are soft-versioned. Nothing is overwritten.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------

create table clinics (
  id              uuid primary key default gen_random_uuid(),
  slug            text not null unique,
  display_name    text not null,
  timezone        text not null default 'Asia/Kolkata',
  languages       text[] not null default '{ta-IN,hi-IN,en-IN}',
  escalation_msisdn text not null,
  config_version  text not null,
  created_at      timestamptz not null default now()
);

create table doctors (
  id           uuid primary key default gen_random_uuid(),
  clinic_id    uuid not null references clinics(id),
  full_name    text not null,
  specialty    text not null,
  active       boolean not null default true
);
create index on doctors (clinic_id, active);

create table opd_slots (
  id           uuid primary key default gen_random_uuid(),
  clinic_id    uuid not null references clinics(id),
  doctor_id    uuid not null references doctors(id),
  starts_at    timestamptz not null,
  ends_at      timestamptz not null,
  capacity     smallint not null default 1,
  held_until   timestamptz,          -- short TTL hold, not a booking
  held_by_call uuid,
  unique (clinic_id, doctor_id, starts_at)
);
create index on opd_slots (clinic_id, starts_at);

-- ---------------------------------------------------------------------
-- Patients — identity only. NO clinical data. Ever.
-- ---------------------------------------------------------------------

create table patients (
  id           uuid primary key default gen_random_uuid(),
  clinic_id    uuid not null references clinics(id),
  msisdn       text not null,
  display_name text,
  dob_hash     text,                 -- hashed. used for identity challenge only
  created_at   timestamptz not null default now(),
  unique (clinic_id, msisdn)
);

-- ---------------------------------------------------------------------
-- Calls and state
-- ---------------------------------------------------------------------

create type call_state as enum (
  'intake','identify','research','draft','validate','repair',
  'approval','execute','audit','wrap',
  'transfer','abandoned','failed','refused'
);

create table calls (
  id            uuid primary key default gen_random_uuid(),
  trace_id      text not null unique,
  clinic_id     uuid not null references clinics(id),
  direction     text not null default 'inbound'
                  check (direction = 'inbound'),   -- C12: outbound prohibited in v1
  ani           text not null,
  patient_id    uuid references patients(id),
  language      text,
  state         call_state not null default 'intake',
  outcome       text,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz
);
create index on calls (clinic_id, started_at desc);

-- Append-only. Replay a run from this table alone.
create table call_state_transitions (
  id            bigserial primary key,
  clinic_id     uuid not null references clinics(id),
  call_id       uuid not null references calls(id),
  from_state    call_state,
  to_state      call_state not null,
  reason        text not null,
  prompt_version text not null,
  model_version  text not null,
  tool_versions  jsonb not null default '{}',
  at            timestamptz not null default now()
);
create index on call_state_transitions (call_id, at);

-- Append-only.
create table call_turns (
  id             bigserial primary key,
  clinic_id      uuid not null references clinics(id),
  call_id        uuid not null references calls(id),
  turn_index     int not null,
  role           text not null check (role in ('caller','agent','system')),
  text_redacted  text not null,      -- post-redaction only. see test_redaction
  language       text,
  asr_confidence real,
  latency_ms     int,
  at             timestamptz not null default now(),
  unique (call_id, turn_index)
);

-- ---------------------------------------------------------------------
-- Consent — DPDP Act 2023. Per-call, purpose-bound, retrievable.
-- CONSENT_STORE is swappable to a registered consent manager (live 2026-11-13).
-- ---------------------------------------------------------------------

create table consent_artefacts (
  id            uuid primary key default gen_random_uuid(),
  clinic_id     uuid not null references clinics(id),
  call_id       uuid not null references calls(id) unique,
  purpose       text not null,
  granted       boolean not null,
  language      text not null,
  notice_text   text not null,       -- exactly what was spoken to the caller
  captured_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Appointments — soft-versioned. Never overwritten, never hard-deleted.
-- ---------------------------------------------------------------------

create table appointments (
  id            uuid primary key default gen_random_uuid(),
  clinic_id     uuid not null references clinics(id),
  patient_id    uuid not null references patients(id),
  doctor_id     uuid not null references doctors(id),
  slot_id       uuid not null references opd_slots(id),
  status        text not null check (status in ('confirmed','cancelled','superseded')),
  version       int  not null default 1,
  supersedes    uuid references appointments(id),
  superseded_by uuid references appointments(id),
  cancel_reason text,
  created_by_call uuid references calls(id),
  created_at    timestamptz not null default now()
);
create index on appointments (clinic_id, status, created_at desc);

-- One live appointment per slot. Makes double-booking a constraint violation,
-- not a race we hope the model avoids.
create unique index appointments_one_live_per_slot
  on appointments (slot_id) where status = 'confirmed';

-- ---------------------------------------------------------------------
-- Agent action audit — append-only. The record of every write attempted.
-- ---------------------------------------------------------------------

create table agent_actions (
  id              bigserial primary key,
  clinic_id       uuid not null references clinics(id),
  call_id         uuid not null references calls(id),
  tool_name       text not null,
  args_redacted   jsonb not null,
  result          text not null check (result in ('success','rejected','error')),
  rejection_reason text,
  idempotency_key text not null unique,
  authorized_by   text not null,     -- 'caller_confirmation' | 'staff:<id>'
  undone_at       timestamptz,
  undone_by       text,
  at              timestamptz not null default now()
);
create index on agent_actions (call_id, at);

-- ---------------------------------------------------------------------
-- Row-level security. Cross-tenant reads return empty, not another clinic.
-- ---------------------------------------------------------------------

alter table clinics                enable row level security;
alter table doctors                enable row level security;
alter table opd_slots              enable row level security;
alter table patients               enable row level security;
alter table calls                  enable row level security;
alter table call_state_transitions enable row level security;
alter table call_turns             enable row level security;
alter table consent_artefacts      enable row level security;
alter table appointments           enable row level security;
alter table agent_actions          enable row level security;

-- ---------------------------------------------------------------------
-- Agent role — least privilege. Enforces the prohibited row (C16) by
-- absence of grant rather than by prompt instruction.
-- ---------------------------------------------------------------------

do $$ begin
  if not exists (select from pg_roles where rolname = 'voicedesk_agent') then
    create role voicedesk_agent nologin;
  end if;
end $$;

grant select on clinics, doctors, opd_slots, patients, appointments to voicedesk_agent;
grant insert on calls, call_state_transitions, call_turns,
                consent_artefacts, appointments, agent_actions to voicedesk_agent;
grant update (state, ended_at, outcome, patient_id, language) on calls to voicedesk_agent;
grant update (held_until, held_by_call) on opd_slots to voicedesk_agent;
grant update (status, superseded_by) on appointments to voicedesk_agent;

-- No DELETE granted on any table.  No grant on any clinical table, because
-- no clinical table exists.  C14 and C16 are unreachable, not discouraged.
