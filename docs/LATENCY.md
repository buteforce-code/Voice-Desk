# Latency Budget

Target from `PROJECT.md` §1.5: **≤1.5s median turn, ≤3.0s P95.**
Industry median in 2026 sits at 1.4–1.7s with P99 at 3–5s. Hitting 1.5s median is competitive; it is not free.

## Budget per turn

| Stage | Budget | Notes |
|---|---|---|
| End-of-turn detection | 60–100ms | Smart Turn v3, see below |
| STT final | 150–250ms | Sarvam Saaras, streaming partials |
| LLM TTFT | 300–500ms | Gemini 2.5 Flash, prefix-cached |
| Tool call (when needed) | 100–300ms | Postgres, no network RAG on the hot path |
| TTS first byte | 150–250ms | Sarvam Bulbul, streaming |
| Network / telephony | 80–150ms | Chennai↔Singapore ≈30–50ms each way |
| **Total** | **~0.9–1.5s** | |

## Techniques adopted

Published 2026 practice compounds to 600–900ms off P95. Adopted in priority order:

1. **Stream every stage.** Sequential STT→LLM→TTS costs 400–800ms of pure waiting. STT partials feed the LLM before the caller finishes; LLM tokens feed TTS at the first sentence boundary; TTS plays while later tokens still generate. Biggest single win.
2. **Prompt prefix caching.** A ~1500-token system prompt drops TTFT from 500–800ms to 200–300ms. Tenant config is static per call, so the prefix is stable by construction.
3. **Smart Turn v3 for end-of-turn.** See below.
4. **Speculative read prefetch.** Start `find_slots` on high-confidence intent before the caller finishes. Saves 200–400ms. **Bounded — see the safety rule.**
5. **Fillers above 800ms.** A short backchannel in the caller's language while generation continues. Never used to mask a tool failure.
6. **No network RAG on the hot path.** Tenant config is loaded into memory at call start. A vector lookup mid-turn is a latency bug, not a feature.

## The safety rule on speculation

> **Speculative execution is permitted only for tools tiered `AUTONOMOUS`.**

Read tools (`find_slots`, `get_clinic_info`) are idempotent and side-effect free — speculating on them is free latency.

Write tools (`hold_slot`, `confirm_booking`, `reschedule`, `cancel`) may **never** be speculatively invoked. Speculating on a write is precisely the excessive-agency failure the risk register exists to prevent: acting on a partially-heard intent, before the caller finished the sentence, before `approval`.

This is enforced in `registry.py`, not by convention: `speculative=True` on a non-autonomous tool raises `SpeculationNotPermitted`.

Most competitors resolve this tension in one of two wrong directions — refuse to prefetch at all and stay slow, or prefetch everything and become unsafe. The tiering makes it a solved problem rather than a tradeoff.

## Smart Turn v3

Pipecat's open-source semantic turn detection. Whisper Tiny encoder + shallow classifier, ~8M params, 8MB int8 ONNX, **12ms CPU inference**, BSD-2. Default turn strategy in current Pipecat.

It reads the **raw waveform**, not the transcript — judging whether a speaker has actually finished a thought from acoustic and linguistic cues together, rather than waiting out a fixed silence timer.

**Why this matters more here than almost anywhere else.** A VAD-only agent interrupts anyone who pauses mid-sentence. Elderly callers pause. Callers switching between Tamil and English mid-sentence pause at the switch. Callers reading a number off a card pause. Every one of those interruptions is a hang-up, and hang-ups are the metric this product exists to move.

**Open question, tracked as an opportunity.** Smart Turn v3 is documented as multilingual but its published language coverage does not confirm Tamil or Hindi. It is BSD-2, fine-tunable, and the training code is public.

Fine-tuning it on Tamil/Hindi **telephony-band** audio is the single most defensible technical edge available to this project — it is the exact failure mode Indian callers hit, no competitor on a managed platform can tune it, and it compounds with Sarvam's telephony-trained ASR.

Sequenced as a **G5 follow-on, not a G4 task**: measure baseline turn-detection accuracy on the code-switch eval slice first. Fine-tune only if the number justifies it. Building it before measuring it would be exactly the mistake the gate order exists to prevent.
