-- Voice Desk — row-level security policies, missing grants, cancel timestamp
--
-- 0001_init.sql ran `enable row level security` on all ten tables and then
-- defined NO policies. Postgres default-denies when RLS is on and no policy
-- matches, and `voicedesk_agent` is not the table owner, so every statement
-- the agent issued returned zero rows or was refused. The grants in 0001 were
-- dead code, and the comment claiming "cross-tenant reads return empty, not
-- another clinic" was true only in the sense that ALL reads returned empty.
--
-- This migration makes tenant isolation real, and fixes three grant gaps that
-- would each have failed at runtime:
--
--   * no SELECT on agent_actions  -> AuditSink.find_replay cannot read, so
--     idempotency replay (a G4 requirement) is impossible
--   * no INSERT on patients       -> confirm_booking cannot record a
--     first-time caller, and appointments.patient_id is NOT NULL
--   * no sequence USAGE           -> every bigserial insert fails
--
-- What has NOT changed: there is still no DELETE grant and no DELETE policy on
-- any table, and no clinical table exists to grant anything on. C14 and C16
-- remain unreachable rather than discouraged.

-- ---------------------------------------------------------------------
-- Tenant key
--
-- The agent's connection carries its tenant in a transaction-local GUC, set
-- by PostgresAdapter._tenant_tx via set_config('app.clinic_id', $1, true).
-- Transaction-local matters: a pooled connection must not leak one clinic's
-- id into the next call's transaction.
--
-- Fail closed. current_setting(..., true) returns NULL rather than raising
-- when the GUC is unset, and `clinic_id = NULL` is NULL, not true — so a
-- code path that forgets to set the tenant reads nothing instead of reading
-- everything. That is the single most important property in this file.
-- ---------------------------------------------------------------------

create or replace function app_clinic_id() returns uuid
  language sql
  stable
  as $$ select nullif(current_setting('app.clinic_id', true), '')::uuid $$;

comment on function app_clinic_id() is
  'Tenant for the current transaction. NULL when unset, which denies every '
  'RLS policy below. Never SECURITY DEFINER — it must run with the caller''s '
  'privileges so it cannot be used to escape the tenant boundary.';

grant usage on schema public to voicedesk_agent;
grant execute on function app_clinic_id() to voicedesk_agent;

-- ---------------------------------------------------------------------
-- Grant corrections
-- ---------------------------------------------------------------------

-- find_replay() reads this table to return a cached result instead of
-- performing a mutation twice. Append-only stays true: insert + select, no
-- update, no delete.
grant select on agent_actions to voicedesk_agent;

-- The session reads its own call row (state, language, patient_id), and a
-- pre-write validator must confirm a consent artefact exists for this call.
grant select on calls, consent_artefacts to voicedesk_agent;

-- A first-time caller has no patients row. confirm_booking upserts one.
-- UPDATE is column-scoped and only ever fills a NULL — see the adapter's
-- coalesce, which never overwrites a name the clinic already holds.
grant insert on patients to voicedesk_agent;
grant update (display_name, dob_hash) on patients to voicedesk_agent;

-- bigserial primary keys need the sequence, or INSERT fails on permission.
grant usage on sequence call_state_transitions_id_seq to voicedesk_agent;
grant usage on sequence call_turns_id_seq to voicedesk_agent;
grant usage on sequence agent_actions_id_seq to voicedesk_agent;

-- CancelOut.cancelled_at had no column to come from. A cancel is an in-place
-- status flip (0001 grants update(status, superseded_by)), not a new version,
-- so the timestamp belongs on the row.
--
-- cancel_reason is added to the update grant for the same reason: 0001 created
-- the column but granted no way to write it, so cancel() would have failed on
-- permission. undo() clears it, because a restored appointment carrying the
-- reason it was once cancelled for reads as a live cancellation to staff.
alter table appointments add column if not exists cancelled_at timestamptz;
grant update (status, superseded_by, cancelled_at, cancel_reason)
  on appointments to voicedesk_agent;

-- call_turns and call_state_transitions stay insert-only for the agent. It
-- appends its own transcript and transitions; it has no reason to read them
-- back mid-call. Staff and the dashboard read them as a separate role. If a
-- G6 output-side validator later needs the transcript, give that validator
-- its own role rather than widening this one.

-- ---------------------------------------------------------------------
-- Policies — read
--
-- `clinics` keys on `id`; every other table keys on `clinic_id`.
-- ---------------------------------------------------------------------

create policy agent_read_own_clinic on clinics
  for select to voicedesk_agent
  using (id = app_clinic_id());

create policy agent_read_doctors on doctors
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_slots on opd_slots
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_patients on patients
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_appointments on appointments
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_calls on calls
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_consent on consent_artefacts
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

create policy agent_read_actions on agent_actions
  for select to voicedesk_agent
  using (clinic_id = app_clinic_id());

-- ---------------------------------------------------------------------
-- Policies — insert
--
-- `with check` is what stops a row being written INTO another tenant. An
-- insert policy without it is a hole, not a policy.
-- ---------------------------------------------------------------------

create policy agent_insert_calls on calls
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id() and direction = 'inbound');

create policy agent_insert_transitions on call_state_transitions
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

create policy agent_insert_turns on call_turns
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

create policy agent_insert_consent on consent_artefacts
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

create policy agent_insert_patients on patients
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

create policy agent_insert_appointments on appointments
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

create policy agent_insert_actions on agent_actions
  for insert to voicedesk_agent
  with check (clinic_id = app_clinic_id());

-- ---------------------------------------------------------------------
-- Policies — update
--
-- Both clauses are required and both pin the tenant: `using` decides which
-- rows may be targeted, `with check` decides what the row may become. Without
-- the second, an update could move a row to another clinic_id — which is the
-- same breach as a cross-tenant read, arriving from the other direction.
--
-- Which COLUMNS may change is already constrained by the column-level grants
-- in 0001 and above. RLS constrains rows; grants constrain columns.
-- ---------------------------------------------------------------------

create policy agent_update_calls on calls
  for update to voicedesk_agent
  using (clinic_id = app_clinic_id())
  with check (clinic_id = app_clinic_id());

create policy agent_update_slots on opd_slots
  for update to voicedesk_agent
  using (clinic_id = app_clinic_id())
  with check (clinic_id = app_clinic_id());

create policy agent_update_patients on patients
  for update to voicedesk_agent
  using (clinic_id = app_clinic_id())
  with check (clinic_id = app_clinic_id());

create policy agent_update_appointments on appointments
  for update to voicedesk_agent
  using (clinic_id = app_clinic_id())
  with check (clinic_id = app_clinic_id());

-- ---------------------------------------------------------------------
-- No DELETE policy is defined for any table, for any role. Combined with the
-- absent DELETE grant, C16 is unreachable by two independent mechanisms.
--
-- Note on ownership: a table's owner bypasses RLS entirely unless the table
-- also sets `force row level security`, which is deliberately NOT used here
-- because migrations and the staff dashboard run as the owner. The consequence is
-- that everything above is void if the voice service connects as the owner
-- instead of as voicedesk_agent — so PostgresAdapter.connect() asserts
-- current_user at startup and refuses to run otherwise. A silent RLS bypass
-- becomes a boot failure.
-- ---------------------------------------------------------------------
