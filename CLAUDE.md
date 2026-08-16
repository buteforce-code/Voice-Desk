# Voice Desk — Agent Contract

You are working inside a Buteforce project. Read this before writing the first file of any session.

## The governing standard

This project is built and judged against:

- `.agents/rules/agent_build_standard.md` — the ten sections
- `.agents/workflows/new_project_lifecycle.md` — gates G0–G9

`.agents` is a link to the Buteforce vault at `D:\Projects\Buteforce\.agents`. Read `PROJECT.md` before touching code.

**A gate is passed when its artifact exists in the repo, not when it feels done.** Do not skip forward. The spine is:

```
business need → risk boundary → workflow + UX → tools/data
→ evaluation → pilot → monitoring → scale
```

## Hard rules for this project

1. **Never add a code path for an outbound call.** C12 in the risk register is prohibited in v1. This is a regulatory boundary (TRAI TCCCPR), not a preference.
2. **Never let the agent produce clinical content.** No advice, triage, symptom interpretation, diagnosis, results, or prescriptions. Enforced by an output-side classifier (C13), not by prompt text.
3. **No clinical columns in the schema.** If a feature seems to need one, stop and escalate.
4. **Prohibited capabilities are enforced by missing code paths and missing DB grants**, never by prompt instruction. A prompt is not a control.
5. **Prompts are written after G3**, not before. State machine and UX first.
6. **Every tool is a narrow API** with a strict schema and server-side authorization. A model deciding it may call a tool is not authorization.
7. **Caller audio is untrusted input.** Fence it as data, strip prompt structure, validate the resulting action downstream. Containment, not detection.
8. **Tenant identity lives in config, never in code.** No clinic name, doctor name, or phone number is hardcoded anywhere. `clinic_id` is on every row from the first migration.
9. **No validator lives only in a prompt** (G6). Required fields, slot existence, double-booking, business hours, consent capture — all in code, independently runnable, tested.
10. **Nothing calls a real patient** until G5 has a committed baseline and G8 records the evidence for the promotion.

## Stack (decided — see PROJECT.md §9)

| Layer | Choice |
|---|---|
| Orchestration | Pipecat (Python, self-hosted) |
| Pipeline | Cascaded STT → LLM → TTS |
| Speech (ta/hi/en) | Sarvam — Saaras STT, Bulbul TTS |
| Reasoning | Gemini 2.5 Flash via Google AI Studio |
| Telephony | Plivo or Exotel (inbound DID) |
| Data | Supabase (Postgres), India region |
| Dashboard | Next.js 16 · React 19 · Tailwind v4 · Framer Motion |
| Deploy | Render or GCP — **never the dev laptop** |

**Do not use Vertex AI** for Gemini. Google AI Studio only, `GOOGLE_GENAI_USE_VERTEXAI=false`. This is a standing org-wide issue — see vault `tech_stack.md`.

**Pin `sarvamai>=0.1.25`** — `pipecat-ai[sarvam]` ships a broken 0.1.21. See PROJECT.md §2.4.

## House conventions

- Python is the pipeline language. TypeScript strict for the dashboard.
- Many small files. 200–400 lines typical, 800 hard max.
- Immutable patterns; no in-place mutation of records. Appointments are soft-versioned, never overwritten.
- Errors handled explicitly at every level. Never swallow.
- No secrets in source. Everything through `.env`, documented in `.env.example`.

## Before calling anything complete

Run the G9 checklist in `.agents/workflows/new_project_lifecycle.md`. A knowledge note must exist in `.agents/knowledge/` with `up:` pointing at `[[MOC Products]]`, and `python tools/graph-lint.py` must be clean.
