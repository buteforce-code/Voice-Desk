# Voice Desk

An inbound voice agent that answers a clinic's phone in Tamil, Hindi and English, and books, reschedules or cancels an OPD appointment in the clinic's system of record — or hands the call to a human with full context.

Nothing else. No outbound calls. No clinical advice.

---

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

**Pre-offline-eval.** Scaffold and definition only. No pipeline code yet. Nothing is deployed and nothing can reach a real patient.

Gates passed: **G0, G1, G2.** See `PROJECT.md`.

## Architecture

```
PSTN ──▶ Plivo/Exotel DID ──▶ Pipecat (Python, Render/GCP, India region)
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              Sarvam STT   Gemini 2.5    Sarvam TTS
              (Saaras)      Flash        (Bulbul)
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

Not yet runnable. Once the pipeline lands:

```bash
cp .env.example .env      # fill in the keys
uv sync                   # installs pipecat + sarvam override
python -m voicedesk.dev   # local dev with a tunnelled DID
```

Deploy target is Railway (Asia Southeast). **Never the dev laptop.** All data at rest stays in Supabase Mumbai — see `PROJECT.md` §2.4.

## Documentation

| File | What's in it |
|---|---|
| `PROJECT.md` | Workflow definition (G1), risk register (G2), decisions |
| `CLAUDE.md` | Contract for AI agents working in this repo |
| `evals/` | The versioned test set — the gate that decides whether the rest was real |
| `tests/` | Deterministic validators and prohibited-capability tests |

Governing standard: `.agents/rules/agent_build_standard.md` · `.agents/workflows/new_project_lifecycle.md`

## Compliance posture

- **Inbound only.** Under TRAI TCCCPR, when the patient initiates the call the outbound commercial framework does not apply. This is the core design constraint, not an accident of scope.
- **DPDP Act 2023.** Purpose-bound consent captured in the first turn and stored as a per-call artefact; India data residency; 90-day recording retention on Indian infrastructure; multilingual notice. The consent store is an interface, so it can be swapped to a registered consent manager when that framework goes live on 2026-11-13.
- **AI disclosure** on the first turn of every call, unconditionally, in the caller's language.
