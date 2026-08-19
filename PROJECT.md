# Voice Desk — Project Definition

> Built and judged against `.agents/rules/agent_build_standard.md`, gated by `.agents/workflows/new_project_lifecycle.md`.
> **Current gate: G5 — cases complete and conformant, baseline blocked on the pipeline.**
> **G6 started early, out of order, and deliberately: see D11.**
> **Rollout stage: pre-offline-eval.** Nothing is deployed. Nothing may call a real patient.

**No clinic is engaged.** This is a portfolio build. A *target prospect profile* is used to keep requirements honest — no contact has been made and no relationship exists. The prospect is named only in the private vault note (`.agents/knowledge/voice_desk.md`), never in this repo. See D5.

Demo tenant is **fictional** (`Meridian Speciality Clinic`). Product shape: single-tenant-shaped, multi-tenant-ready.

---

## 1. The workflow (G1)

### 1.1 User and business outcome

- **Primary user:** a patient (or their family member) calling the clinic's published number.
- **Secondary user:** the front-desk staffer who answers that number today and is interrupted mid-task to do it.
- **Business outcome:** recover the inbound calls currently lost to busy signal, hold queue and voicemail — 22–38% of patient leads at Indian clinics, 30–40% of calls at peak for mid-size hospitals — and convert them into confirmed OPD appointments in the clinic's system of record.
- **Not the outcome:** replacing the front desk. The desk stops answering the *routine* call, not every call.

### 1.2 Inputs, outputs, source of truth

| | |
|---|---|
| **Inputs** | Inbound PSTN audio (8kHz telephony); caller number (ANI); tenant config (doctors, specialties, OPD hours, consult fees, prep instructions, escalation number); live slot availability |
| **Outputs** | An appointment record created / rescheduled / cancelled; a transcript + immutable audit row; an SMS/WhatsApp confirmation; or a warm transfer carrying context |
| **Source of truth** | The clinic's scheduling system where an API exists. Where none exists, the Voice Desk `appointments` table is the designated SoT for that tenant, reconciled to the clinic's register daily. **Never the model's conversational memory.** |

### 1.3 What the agent may read / draft / recommend / execute

Full tiering in §2. Summary: it may **read** schedule and tenant config freely, **execute** booking-class writes only through narrow authorized tools with idempotency and undo, and **never** execute anything in the prohibited row.

### 1.4 What it must never do autonomously

- Give clinical advice, interpret symptoms, triage urgency, or suggest a specialty based on described symptoms.
- Discuss diagnoses, test results, or prescriptions — including confirming that a report exists.
- Place an outbound call. **Not reachable in code in v1.**
- Take payment or card details.
- Cancel or reschedule without verified caller identity.
- Store clinical content. The schema has no column for it.
- Claim to be human. AI disclosure is unconditional, first turn, every call.

### 1.5 Success target

| Metric | Target | Measured by |
|---|---|---|
| Inbound calls resolved without human transfer | **≥ 70%** | G5 eval set + production trace |
| Booking accuracy (right patient · right doctor · right slot) | **≥ 98%** | G5 eval set |
| Median turn latency | **≤ 1.5s** | per-turn trace timing |
| P95 turn latency | **≤ 3.0s** | per-turn trace timing |
| Correct language selection (ta / hi / en, incl. code-switch) | **≥ 95%** | G5 code-switch slice |
| Unauthorized writes | **0** | G6 validators + audit log |
| Clinical-advice responses across red-team set | **0** | G5 malicious/edge slice |
| Cost per resolved booking | **≤ ₹12** | G7 cost ledger |

A run that misses the accuracy or clinical-advice targets is a failed run regardless of how natural it sounded.

### 1.6 The job, named narrowly

> **One run = one inbound call.**
> It **succeeded** if the caller's intent — book, reschedule, cancel, or ask a listed FAQ — was correctly resolved in the system of record, *or* correctly transferred to a human with context, **and** no prohibited action occurred.
> It **failed** otherwise.

A stranger holding the transcript, the resulting appointment row and the audit log can decide this without asking anyone.

---

## 2. Risk register (G2)

**Tiers** per `agent_build_standard.md` §2: Autonomous · Human review · Explicit approval · Prohibited/dual.

| # | Capability | Tier | Control that enforces the tier |
|---|---|---|---|
| C1 | Read OPD slot availability | Autonomous | Read-only DB role; tenant-scoped row filter |
| C2 | Read tenant config (doctors, hours, fees, prep) | Autonomous | Read-only; config is signed and versioned |
| C3 | Answer FAQ from tenant config | Autonomous | Grounded retrieval only; refusal path when unmatched. No free generation on factual claims |
| C4 | Identify caller against patient record | Autonomous (read) | Match on ANI + DOB challenge; 3-attempt cap then transfer |
| C5 | Propose a slot to the caller | Autonomous | Proposal is speech only; no write occurs |
| C6 | **Create** appointment | Explicit approval → approval-gated autonomy at G8 | Caller verbal confirmation captured + `confirm_booking()` server-side authz + idempotency key + audit row + undo window |
| C7 | **Reschedule** appointment | Explicit approval | C4 identity verified; original row soft-versioned, never overwritten; undo |
| C8 | **Cancel** appointment | Explicit approval | C4 identity verified; soft-delete only; undo; cancellation reason recorded |
| C9 | Send confirmation SMS/WhatsApp | Human review → autonomous once templated | Pre-registered DLT template; content from struct fields only, never model-authored |
| C10 | Warm transfer to human | Autonomous | Always permitted. Transfer is the safe default on any uncertainty |
| C11 | Write transcript + audit row | Autonomous | Append-only table; no delete grant |
| C12 | **Outbound call** | **Prohibited (v1)** | No dialer credential issued. No outbound code path exists. Unlocks only behind DLT + 1600-series + consent artefact store |
| C13 | **Clinical advice / triage / symptom interpretation** | **Prohibited** | Hard refusal + immediate transfer offer; G6 deterministic classifier on agent output, not only on prompt |
| C14 | **Disclose diagnosis, test results, prescriptions** | **Prohibited** | Not retrievable — the agent has no grant to any clinical table |
| C15 | **Take payment / card details** | **Prohibited** | No payment tool exists. Detected card-number pattern in ASR triggers redaction + transfer |
| C16 | Delete any record | **Prohibited** | No DELETE grant on any table for the agent role |
| C17 | Change permissions or tenant config | **Prohibited** | Config is deploy-time, not runtime. No tool exists |

### 2.1 Prohibited-row enforcement

Every prohibited capability is enforced by **absence of a code path or a database grant**, not by prompt instruction. A prompt saying "never give medical advice" is not a control; C13's output-side classifier is. Verified by test in `tests/test_prohibited.py` before any deploy.

### 2.2 Time-boxed veto windows

The undo window on C6/C7/C8 is **autonomy with a grace period, not approval.** Recorded as such. At stage `approval-gated execution` the human confirmation is synchronous and blocking; the undo window is an additional safety net, never a substitute.

### 2.3 Regulatory obligations

| Obligation | Source | How it binds this build |
|---|---|---|
| Inbound calls exempt from outbound commercial framework | TRAI TCCCPR | **The core design constraint.** v1 is inbound-only precisely to stay in this zone |
| 1600/140x series, DLT registration, template pre-registration, daily DND scrub, 9am–9pm window, AI disclosure | TRAI TCCCPR (outbound) | Not applicable while C12 is prohibited. Blocks any future outbound feature |
| Penalty ₹25,000 per upheld complaint; DLT suspension on repeat | TRAI | Rationale for keeping C12 prohibited until infrastructure exists |
| Purpose-bound consent, retrievable per-call consent artefact | DPDP Act 2023 | Consent captured in first turn, stored as a row keyed to call trace ID |
| India data residency; 90-day recording retention on Indian infra | DPDP / TRAI | Deploy region locked to India. No US-region storage |
| Multilingual notice at point of care | DPDP Rules | Disclosure line rendered in ta / hi / en |
| Health data is sensitive personal data; penalties to ₹250 crore | DPDP Act 2023 | Justifies C14/C16 prohibitions and the no-clinical-column schema |
| Consent-manager framework operational **2026-11-13** | DPDP Rules | **3 months out.** Consent store must be swappable to a registered consent manager. Designed as an interface from day one |

### 2.4 Technical risks

| Risk | Impact | Mitigation |
|---|---|---|
| `pipecat-ai[sarvam]` pins `sarvamai==0.1.21`, missing `saaras:v3` `mode`/`prompt` support; `set_prompt` raises (pipecat #3783) | Blocks best-in-class Indic STT | Override to `sarvamai>=0.1.25`; set language explicitly rather than trusting `en-IN` default; pin and test on upgrade |
| Single ASR/TTS vendor | Provider outage kills all calls | Provider abstraction from day one; Deepgram/Google fallback wired at G7 |
| Prompt injection via caller speech | Unauthorized action | Containment not detection: caller audio fenced as data, tool authz server-side, action validated downstream (G6) |
| 8kHz telephony + code-switch degrades ASR | Wrong bookings | Sarvam Saaras (trained on Indian telephony); dedicated G5 code-switch eval slice |
| Latency creep from RAG lookup | Caller hangs up | Tenant config preloaded in memory; no network RAG on the hot path |
| Clinic has no scheduling API | No write-back | Flat-file + human-confirm fallback mode; designated-SoT table with daily reconciliation |
| **Railway has no India region** (US West/East, EU West, Asia Southeast only) | Compute sits in Singapore, not India | Acceptable: DPDP §16 permits processing outside India absent a blacklist, and TRAI residency binds *recordings at rest*, not compute. **All data at rest stays in Supabase Mumbai (`ap-south-1`), non-negotiable.** Chennai→Singapore RTT ≈30–50ms, no material latency cost. Revisit if an India region ships |
| Railway enforces a **15-minute connection limit** | Long calls drop mid-stream | `MAX_CALL_DURATION_SEC=420` (7 min) sits well inside it. Becomes a real constraint only if hold-music or long-queue features are ever added |
| Render free tier cold-starts at 50s+ (documented in vault `tech_stack.md`) | **A cold start is a dead call** — the caller hangs up long before the agent answers | Primary reason Railway is preferred over Render here. Whichever host, the voice service must never scale to zero once a DID is live |

### 2.5 Named owners

| Role | Owner |
|---|---|
| Product | Dhyaneshwaran |
| Security | Dhyaneshwaran |
| Data / privacy (DPDP) | Dhyaneshwaran |
| Business outcome | Dhyaneshwaran |

One person holds all four today. Written down anyway, per G2.

**Clinical sign-off on the C13/C14 boundaries is unsigned and cannot be signed by Buteforce.** No clinician has reviewed them. This is an open governance gap, not a completed item: it blocks any live call with a real patient, and must be countersigned by the first real clinic before rollout stage `shadow mode`.

---

## 3. State machine and UX (G3) — passed

| Requirement | Artifact |
|---|---|
| States enumerated and stored in the database, not inferred | `docs/STATE_MACHINE.md` · `db/migrations/0001_init.sql` (`call_state` enum, `calls.state`, append-only `call_state_transitions`) |
| UI shows current step · sources · draft-vs-final diff · uncertainty · approve/reject/edit/retry/undo · full history | `docs/DASHBOARD_UX.md` §G3 required elements |
| Undo exists for every reversible executed action before that action ships | `docs/STATE_MACHINE.md` §Undo. Enforced structurally by soft-versioning (`supersedes` / `superseded_by`) and no DELETE grant |

Key invariant: **`execute` is reachable only from `approval`.** No other edge exists in the machine.

Prompts may now be written. Not before.

## 4. Tools as narrow APIs (G4) — passed

| Requirement | Artifact |
|---|---|
| One function per action, strict schema in and out | `tools/schemas.py` — Pydantic `extra="forbid"`, `frozen=True`. Eight tools, no `run_query`/`execute` escape hatch |
| Server-side authorization on every tool | `tools/registry.py::_authorize` — `ToolContext` is built by the session; the model cannot assert its own `clinic_id`, `state` or `approval_token` |
| Least privilege credentials | `voicedesk_agent` role, `db/migrations/0001_init.sql` |
| Rate limits | 25 tool calls per call, 8 per tool per call |
| Idempotency | Required on every mutating tool; replay returns cached result |
| Audit log | One `agent_actions` row per **attempt**, rejections included |
| Sandbox / dry-run | `ToolContext.dry_run`, default `true` |
| Untrusted content bounded, stripped, fenced as data | `security/fencing.py` — containment not detection, per-call nonce envelope |

Adapter seam: `adapters/base.SchedulingAdapter` Protocol. `PostgresAdapter` (`adapters/postgres.py`) is the designated-SoT implementation, added 2026-08-17 — until it existed, every tool called a method on a bare Protocol and nothing could execute. `HmisAdapter` remains **deliberately unimplemented** — no clinic is engaged, so an adapter written against a guessed API shape would be fiction.

### D7 — Speculative execution is tiered (2026-08-16)

Prefetching reads on high-confidence intent saves 200–400ms. Prefetching a write means acting on a half-heard sentence before the caller finished — textbook excessive agency.

Rule: **speculation requires `AUTONOMOUS` tier AND `side_effect_free`.** `hold_slot` is autonomous but mutating, so it is excluded. Enforced by `ToolSpec.speculatable` / `SpeculationNotPermitted`, not by convention.

### D8 — Smart Turn v3 for end-of-turn detection (2026-08-16)

Pipecat's open-source semantic turn model — reads the raw waveform rather than the transcript, 12ms int8 CPU inference, BSD-2, already the Pipecat default.

A VAD-only agent interrupts anyone who pauses mid-sentence. Elderly callers pause; code-switchers pause at the switch; people reading a number off a card pause. Each interruption is a hang-up, and hang-ups are the metric this product exists to move.

Its published language coverage does not confirm Tamil or Hindi. **Fine-tuning it on Tamil/Hindi telephony-band audio is the most defensible technical edge available here** — no competitor on a managed platform can tune it. Sequenced as a **G5 follow-on**: measure baseline turn accuracy on the code-switch slice first, fine-tune only if the number justifies it. See `docs/LATENCY.md`.

## 5. Evaluation (G5) — cases complete, baseline blocked on the pipeline

**57 cases**, all six classes at target: normal 8 · edge 9 · ambiguous 9 · bad_input 9 · malicious 12 · codeswitch 10. `python -m evals.run --validate` green.

### D9 — Schema revision 2 (2026-08-16)

Writing the cases surfaced **ten defects in the eval schema and one in production**, all before a baseline existed. Had a baseline been committed first, every one would have been frozen in and all later baselines rendered incomparable — the single thing a baseline exists to prevent. This is the argument for G5 sitting where it does in the gate order.

Two defects meant a case **could not catch the bug it was written for**:

- `max_turns` had undefined scope. If it counted caller turns only, an agent looping re-prompts forever never tripped it — the exact bug `edge-008` hunts. Now `max_total_turns`, explicitly all turns.
- No effort floor, so a `transferred` case passed on an agent that transferred instantly without trying. Now `min_agent_turns`.

Others: `must_not` implied it drove pass/fail and never did (renamed `probes`, honest docstring, universal enforcement kept); tool names were unvalidated and six references to three non-existent tools shipped (now checked against the live registry); `language_used` was a per-case string for a per-turn behaviour (now `Turn.expect_language` + turn-level counters); injected faults lived in YAML comments the harness cannot read (now `EvalCase.inject`, and a run missing its declared fault is **void**, not passing).

`Baseline.schema_revision` now exists so the harness refuses to diff across revisions rather than printing a misleading delta.

Added `Violation.FABRICATED_SUCCESS` — the whole `bad_input` slice was written around an agent inventing a successful outcome when the system failed, and there was no name for it.

Added `rots_on_model_change`: cases depending on a specific model weakness (e.g. ASR reliably mangling a Tamil surname) die when that weakness is fixed. That is **case rot**, not a regression, and the two look identical in a diff.

### D10 — `find_appointments` added to the registry (2026-08-16)

**A missing tool, found by eval authors rather than by design review.** `RescheduleIn` and `CancelIn` both require an `appointment_id`; nothing in the registry produced one. The agent was being asked to change a booking it had no way to locate. Two independent case authors inferred the tool must exist and wrote against a guessed name.

Added as AUTONOMOUS / side-effect-free, with `identity_verified: Literal[True]` — reading a patient's bookings is reading their health data, and without the gate the tool becomes a way to enumerate whether an arbitrary number has appointments here.

### Production bug found by `badinput-008`

`Msisdn` was `^\+?[1-9]\d{7,14}$` — 8 to 15 digits. An Indian mobile is exactly 10, so a **9-digit number passed**: a real number with one digit dropped by ASR, which is the most likely transcription failure on a noisy line. It would have validated cleanly, been written to the patient record, and sent the confirmation SMS to nobody. Now `^(?:\+?91)?[6-9]\d{9}$`.

### Still open before a baseline

- Scoring cannot express "forbid an INTENT", only a tool. Unanticipated routes rest entirely on violation detection.
- Several cases require a judge that inspects agent **utterances**, not just tool calls. `ambiguous-009` is the clearest: an agent can leak a third party's appointment aloud while every tool call it made was legitimate. Tool-call auditing cannot see that failure at all.
- Fixture validity: `edge-006` needs audio pre-measured to actually produce diverging ASR decodes; TTS-plus-noise usually decodes too well to test anything.

## 5a. Evaluation (G5) — remaining

The baseline needs a running pipeline, which needed a working adapter, which needed tenant
isolation to actually exist. That chain is the subject of D11.

## 6. Validators (G6) — 2 of 10 modules, both blocking in CI

| Module | State |
|---|---|
| `test_prohibited.py` | ✅ 82 tests. C12–C17 unreachable, approval boundary, speculation tiering, redaction, no real clinic name in the repo |
| `test_tenant_isolation.py` | ✅ 37 tests. Policy shape, fail-closed tenant function, adapter self-scoping, RLS-bypass refusal |
| `test_slot_validity.py` · `test_double_booking.py` · `test_identity.py` · `test_consent.py` · `test_clinical_guard.py` · `test_injection.py` · `test_redaction.py` · `test_undo.py` | not started |

Both existing modules run on a bare checkout — no database, no model, no telephony account. That
is what lets them be gates rather than something skipped when the environment is inconvenient.

## 7. Operations (G7) — not started
## 8. Rollout stage (G8)

```
▶ pre-offline-eval  →  offline eval  →  internal sandbox  →  shadow mode
   →  draft-only  →  approval-gated execution  →  limited autonomous low-risk
```

Current stage: **pre-offline-eval.** Evidence required to promote: a populated `evals/` set with a committed baseline. Cases are populated and conformant; the baseline is the one missing artifact.

---

## 9. Decisions

### D1 — Orchestration: Pipecat, self-hosted (2026-08-16)

Chosen over Vapi/Retell.

- Pipecat has **first-party Sarvam support** (`pipecat-ai[sarvam]`, streaming WS TTS + STT); Sarvam publishes a Pipecat production guide; Plivo publishes a Plivo→Pipecat→Sarvam guide. The whole path is documented and first-party.
- Vapi reaches Sarvam only via a custom-transcriber WebSocket and custom-TTS webhook that we would host ourselves — so its "no infrastructure" advantage disappears at the exact component that matters most, while still charging per-minute margin.
- G7 requires prompt/model/tool version stamped per run, cost ceilings, kill switch and provider fallback. Managed platforms abstract these away.
- Above 10K min/month the framework path undercuts managed by 60–80%.
- `services_overview.md` states Buteforce does not ship "generic chatbots you can buy off Voiceflow." A managed wrapper would contradict the positioning this project exists to prove.

The dev laptop is not the deploy target and was never the constraint.

### D1a — Host: Railway (2026-08-16)

Chosen over Render and GCP. No GCP credit available. Railway's usage-based pricing suits a project that is idle most of its life, and Render's free-tier 50s cold start is fatal for voice — a caller hangs up long before the agent speaks.

Accepted with two caveats, both recorded in §2.4: no India region (Singapore is nearest; **data at rest stays in Supabase Mumbai regardless**), and a 15-minute connection cap (7-minute call ceiling sits inside it).

### D5 — No clinic is engaged. The demo tenant is fictional (2026-08-16)

The target prospect is a **real organisation that has not been contacted.** It is used only as a private profile to keep requirements grounded, and it is named only in the vault — not here, not in code, not in a test fixture.

Every public artifact — demo, replay page, screenshots, case study — uses a **fictional clinic** (`Meridian Speciality Clinic`) with fictional doctors and fictional slots. No real hospital's name, branding, phone number or doctor roster appears in anything shippable.

Two reasons, and the first is sufficient on its own:

1. A public demo branded as a real named hospital's booking line implies a working relationship and an endorsement that do not exist, and would put a real clinic's name on an unreviewed clinical-boundary system. Not shippable at any quality bar.
2. It is what the project was asked for anyway: a **white-label blueprint** that copies to any clinic in any market. Hard-binding the demo to one hospital is the thing that would stop it being reusable.

The prospect stays in the private vault note. If they later become a real design partner, that is a config file and a signed clinical review, not a rebuild.

### D6 — Telephony: Plivo (2026-08-16)

API-first, publishes a Plivo→Pipecat→Sarvam integration guide, inbound DID ₹0.40–0.90/min. Exotel is the stronger enterprise incumbent and remains the swap target if an enterprise clinic later demands it — which is why telephony sits behind the same provider abstraction as STT/TTS.

**No DID is purchased yet.** With no clinic and no eval baseline, there is nothing to call. The spend happens at rollout stage `internal sandbox`, not before.

### D2 — Cascaded pipeline, not speech-to-speech (2026-08-16)

STT → LLM → TTS. Production default in 2026 for tool-calling reliability and observability. This build needs auditability more than it needs sub-500ms.

### D3 — Inbound only in v1 (2026-08-16)

Not a scope cut — the compliance wedge. See §2.3.

### D4 — Languages: Tamil + Hindi + English, with code-switch (2026-08-16)

Chennai has a large Hindi-speaking population. Code-switch within a single utterance is the norm, not the edge case, and is a first-class eval slice.

### D11 — G6 started before G5 finished, because G5 could not run (2026-08-17)

A review of the repo against its own claims found that **row-level security was enabled on all ten
tables with zero policies defined.** Postgres default-denies in that state and `voicedesk_agent` is
not the table owner, so every statement the agent issued returned nothing. The grants in
`0001_init.sql` were dead code, and the comment promising "cross-tenant reads return empty, not
another clinic" was true only because *all* reads returned empty.

That is not a G6 validator problem, it is a G3 schema defect — but it surfaced as the reason the
G5 baseline could not be produced. The dependency chain runs backwards through the gate order:

```
G5 baseline  ->  needs a pipeline
             ->  needs an adapter that can execute      (G4, contracts only)
             ->  needs tenant isolation that works      (G3, enabled but empty)
             ->  needs a test that would have caught it (G6, not started)
```

Fixed in `0002_rls_policies.sql`, with `tests/test_tenant_isolation.py` written first so the defect
cannot return silently. Writing the two test modules found four more defects that no amount of
reading would have:

- **A 16-digit card number was redacted as an Aadhaar number, leaving its last four digits in the
  transcript.** Aadhaar is 12 digits in 4-4-4 and ran first, so it consumed the card's first twelve.
  Partial card data persisted, and the audit row misnamed what the caller said. Longest pattern
  first now (`security/fencing.py`).
- **Authorization ran after schema validation**, so an unauthorized write with malformed arguments
  was audited as `invalid_arguments`. The audit log exists to show attempted unauthorized writes;
  that ordering lost exactly that signal. `_authorize` reads only `ctx` and `spec`, so it now runs
  first (`tools/registry.py`).
- **Three grants the code depends on were missing**: no SELECT on `agent_actions` (idempotency
  replay cannot read), no INSERT on `patients` (a first-time caller cannot be recorded, and
  `appointments.patient_id` is NOT NULL), no sequence USAGE (every append-only insert fails).
  Each would have failed at runtime, not at deploy.
- **`CancelOut.cancelled_at` had no column to come from**, and `cancel_reason` had no update grant.

The CI job asserting the prohibited row *would* have failed since G4: its grep matched
`_PROHIBITED_BY_ABSENCE`, the frozenset that lists the forbidden call names so they can be checked
for. A guard that fires on its own guard list is a guard someone disables. It now matches call
sites only, and `continue-on-error` is off every step that guards a real control.

**It would have failed, not did: the repo has no git remote and CI has never executed.** Every
"blocking CI job" claim in this document describes configuration, not observed behaviour, until
the repo is pushed to a host that runs workflows. That is a live gap, not a footnote — G5 and G6
both rest on the suite running on every change.

**The lesson is the one G5 already taught in D9, arriving from the other side.** D9 found ten
defects by writing eval cases before committing a baseline. This found six by writing validators
before running an eval. In both cases the artifact that catches defects is the one that has to
exist *first*, and in both cases the gate marked ✅ was marked so on its design document rather
than on anything executable.

---

## Log

- **2026-08-16** — G0 scaffold. G1 and G2 written. D1–D4 recorded.
- **2026-08-17** — Repo audited against its own claims. RLS policies written (`0002`), `PostgresAdapter` built, first two G6 validator modules landed (119 tests, blocking in CI). Six defects found and fixed; D11 records them. Docs resynced: eight tools, not six.
