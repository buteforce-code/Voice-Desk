# Voice Desk

An inbound voice agent that answers a clinic's phone in Tamil, Hindi and English, and books, reschedules or cancels an OPD appointment in the clinic's system of record — or hands the call to a human with full context.

Nothing else. No outbound calls. No clinical advice.

---

## Try it locally

No API keys, no database, no telephony:

```bash
uv sync --all-extras --dev
uv run python -m voicedesk.console
```

Walks a booking end to end, then shows what happens when a write is attempted
from the wrong state, when someone asks for another patient's appointments,
when the caller asks for medical advice in English and Hindi, when identity
fails three times, and what the audit log recorded for all of it.

Every path is **scripted** — there is no model in the loop. That makes it a
demonstration of the controls, not of the agent's judgement. For the agent's
judgement, see the eval suite below; for a conversation, `voicedesk.chat`.

```bash
uv run pytest tests/ -q          # 480 tests, no secrets required
uv run python -m evals.run --validate
```

## Talk to it

One key — `GOOGLE_AI_API_KEY` or `OPENROUTER_API_KEY` in `.env`. No database, no
telephony.

```bash
uv run python -m voicedesk.chat --trace --audit
```

Real model, real tool calling, real state machine, real clinical guard, real
audit trail. You type instead of talking; STT and TTS sit on either end of
exactly this loop, which is why it is the cheapest place to find a booking bug.

## Score it

```bash
uv run python -m evals.run --validate     # case conformance. no key, no network
uv run python -m evals.run --run          # 58 cases against the live model
```

Drives every case through the same loop and scores it: did the caller's intent
resolve in the register, was every factual claim traceable to a tool result,
did a prohibited action occur. A violation fails a case outright — booking the
right slot while giving medical advice is a failure, not a partial pass.

Two things the report always says out loud. A case the harness cannot stage is
**SKIP, never PASS**. A case that declares a backend failure and does not get
one is **VOID, never PASS**.


## Why this exists

Indian clinics lose **22–38% of patient leads** to missed calls. Mid-size hospitals miss **30–40%** of calls at peak. About a third of new patients who hit voicemail hang up and dial the next provider.

The front desk is not bad at its job. It is one person with one phone line and a waiting room.

## Scope

| | |
|---|---|
| **In** | Inbound calls · book / reschedule / cancel · listed FAQs (hours, address, fees, prep) · warm transfer |
| **Out** | Outbound calls · clinical advice, triage, symptoms · diagnoses, results, prescriptions · payments |

The out-of-scope list is enforced by missing code paths and missing database grants, not by prompt instructions. See `PROJECT.md` §2.

## Status

**Pre-offline-eval.** Nothing is deployed and nothing can reach a real patient.

Gates passed: **G0–G4**, and **G6** (9 validator modules, 480 tests, blocking in CI). **G5** has a
scoring harness and a committed baseline — `3/58` at `--repeat 3`, and **0% booking accuracy**. That
is a starting point, not a passing grade, and it is the point of having measured it. `PROJECT.md` carries the detail and the gaps.

## Architecture

```
PSTN ──▶ Plivo/Exotel DID ──▶ Pipecat (Python, Render/GCP, India region)
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              Sarvam STT   Gemini /     Sarvam TTS
              (Saaras)     OpenRouter    (Bulbul)
                                 │
                                 ▼
                   narrow tools (server-side authz)
                   find_slots · hold_slot · confirm_booking
                   reschedule · cancel · transfer
                                 │
                                 ▼
                  Supabase (Postgres, India) — append-only audit
                                 │
                                 ▼
                    Next.js dashboard — transcript, sources,
                    draft-vs-final diff, confidence, undo, history
```

Cascaded STT→LLM→TTS rather than speech-to-speech: this build needs tool-calling reliability and an audit trail more than it needs sub-500ms.

## Multi-tenancy

Single-tenant-shaped, multi-tenant-ready. `clinic_id` on every row from the first migration. All clinic identity — name, doctors, specialties, languages, greeting, hours, escalation number — lives in config, never in code. Adding a clinic is a config file, not a fork.

## Running it

The text pipeline runs today — see **Talk to it** and **Score it** above. Speech
and telephony are what remain:

```bash
cp .env.example .env      # fill in the keys
uv sync                   # installs pipecat + sarvam override
python -m voicedesk.dev   # local dev with a tunnelled DID — not yet built
```

Deploy target is Railway (Asia Southeast). **Never the dev laptop.** All data at rest stays in Supabase Mumbai — see `PROJECT.md` §2.4.

## Documentation

| File | What's in it |
|---|---|
| `PROJECT.md` | Workflow definition (G1), risk register (G2), decisions |
| `CLAUDE.md` | Contract for AI agents working in this repo |
| `evals/` | The versioned test set — the gate that decides whether the rest was real |
| `tests/` | Deterministic validators and prohibited-capability tests |
| `evals/README.md` | How the harness works and what each metric actually counts |

Governing standard: `.agents/rules/agent_build_standard.md` · `.agents/workflows/new_project_lifecycle.md`

## Compliance posture

- **Inbound only.** Under TRAI TCCCPR, when the patient initiates the call the outbound commercial framework does not apply. This is the core design constraint, not an accident of scope.
- **DPDP Act 2023.** Purpose-bound consent captured in the first turn and stored as a per-call artefact; India data residency; 90-day recording retention on Indian infrastructure; multilingual notice. The consent store is an interface, so it can be swapped to a registered consent manager when that framework goes live on 2026-11-13.
- **AI disclosure** on the first turn of every call, unconditionally, in the caller's language.
