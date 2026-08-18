# Tools (G4)

> Prefer `confirm_booking(slot_id, msisdn, name)` over "access to the scheduler."

Eight tools. No general-purpose escape hatch — there is no `run_query`, no `call_hmis`, no `execute`.

> Was six at G4. `find_appointments` was added by D10 when eval authors found that `reschedule`
> and `cancel` both require an `appointment_id` no tool produced; `transfer_to_human` was always present
> and simply uncounted.

## The registry

| G4 requirement | Where |
|---|---|
| One function per action, strict schema in and out | `tools/schemas.py` — Pydantic, `extra="forbid"`, `frozen=True` |
| **Server-side** authorization on every tool | `registry.ToolRegistry._authorize` |
| Least privilege | `voicedesk_agent` role, `db/migrations/0001_init.sql` |
| Rate limits | 25 calls/call, 8 per tool/call |
| Idempotency | Required on every mutating tool; replay returns the cached result |
| Audit log | One `agent_actions` row per **attempt**, including rejections |
| Sandbox / dry-run | `ToolContext.dry_run`, default `true` |
| Untrusted content bounded, stripped, fenced | `security/fencing.py` |

## The tools

| Tool | Tier | Mutates | Speculatable |
|---|---|---|---|
| `get_clinic_info` | autonomous | no | ✅ |
| `find_slots` | autonomous | no | ✅ |
| `find_appointments` | autonomous | no | ❌ identity-gated |
| `hold_slot` | autonomous | **yes** | ❌ |
| `confirm_booking` | explicit approval | yes | ❌ |
| `reschedule_appointment` | explicit approval | yes | ❌ |
| `cancel_appointment` | explicit approval | yes | ❌ |
| `transfer_to_human` | autonomous | no | ✅ |

## Three design choices worth naming

**1. Authority never comes from model output.**
`ToolContext` is built by the call session. The model cannot assert its own `clinic_id`, its own `state`, or its own `approval_token`. An `explicit_approval` tool checks `ctx.state == "approval"` and a token the state machine issued — so the sole path to a write runs through the one state that requires the caller to have said yes.

**2. Verification is in the type, not the prompt.**
`RescheduleIn.identity_verified: Literal[True]`. There is no way to express an unverified reschedule that survives validation. "I forgot to check identity" becomes a schema error at the boundary rather than a bug found in production.

**3. Speculation is tiered, not banned and not free.**
Prefetching `find_slots` on high-confidence intent saves 200–400ms. Prefetching `confirm_booking` would mean acting on a half-heard sentence before the caller finished — textbook excessive agency.

The rule: **speculation requires `AUTONOMOUS` tier AND `side_effect_free`.** `hold_slot` is autonomous but mutating, so it is excluded. `registry.ToolSpec.speculatable` derives this; `SpeculationNotPermitted` enforces it.

Most competitors land on one of the two wrong answers — never prefetch and stay slow, or prefetch everything and become unsafe. Tiering makes it a solved problem instead of a tradeoff.

## Untrusted content

Caller speech is transcribed audio going straight into an LLM prompt — the most untrusted input in the system.

`security/fencing.py` takes the containment stance from the standard §5, not detection:

1. **Bound** — 1200 chars/utterance, 8000 total.
2. **Strip** — remove role markers, `<|…|>`, `[INST]`, fences, rules. Caller text cannot forge a role boundary.
3. **Fence** — wrap in a **per-call nonce** envelope. A static delimiter can be guessed and closed early; a nonce cannot.
4. **Validate downstream** — the actual control. Whatever the model concludes, `registry.py` authorizes server-side.

Redaction of card and Aadhaar patterns runs *before* text reaches a prompt, a log, or the transcript table. Callers volunteer card numbers unprompted, and C15 is prohibited.

## The adapter seam

`adapters/base.SchedulingAdapter` is a Protocol. Everything above it — tools, state machine, validators, evals — is written against the interface, never against Postgres.

- `PostgresAdapter` — designated source of truth for clinics with no API. **Built** (`adapters/postgres.py`). Every statement names `clinic_id` in its own WHERE or CHECK even though RLS already confines the transaction, and `connect()` refuses to start as a role that bypasses RLS — a superuser connection would make every policy inert, silently.
- `HmisAdapter` — Halemind / KareXpert / Practo Ray / SoftClinic. **Deliberately unimplemented.** No clinic is engaged; writing an adapter against a guessed API shape would be fiction. When a real clinic exists it implements this Protocol and nothing above changes.

## Prohibited by absence

No tool exists for outbound calls (C12), clinical advice (C13), results or prescriptions (C14), payment (C15), deletion (C16), or config changes (C17).

Not "a tool that refuses" — **no tool.** Unknown tool names still produce a rejected audit row, so an attempt is visible without a code path existing.

`tests/test_prohibited.py` asserts this as a **blocking CI job** — genuinely blocking as of 2026-08-17, when the tests were written and `continue-on-error` came off the step.
