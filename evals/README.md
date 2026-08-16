# Evals

**G5 — the gate most often skipped, and the one that decides whether the rest was real.**

Empty at G0 by design. Populated before anything is promoted past `internal sandbox`.

Trolls died here (`.agents/knowledge/trolls_build_audit.md`: strong validators, no evaluation layer — G5 blocked everything else). Do not repeat it.

## Layout

```
evals/
  cases/
    normal/       the boring 80% — book, reschedule, cancel, listed FAQ
    edge/         hangups, dead air, wrong number, background noise, 8kHz clipping,
                  two doctors with the same surname, caller is a family member
    ambiguous/    "sometime next week", "the lady doctor", "same as last time",
                  unclear which of three branches
    bad_input/    caller wants a specialty the clinic does not have, DOB mismatch,
                  slot taken mid-call, HMIS returns 500
    malicious/    prompt injection via speech, social-engineering a cancellation,
                  coaxing clinical advice, extracting another patient's record
    codeswitch/   Tamil/Hindi/English mixed within a single utterance — a
                  first-class slice, not an edge case
  baseline/
    latest.json   committed. a regression is visible as a number
  run.py          the harness
```

## Metrics recorded per run

Per `agent_build_standard.md` §6 — not "did it sound good":

| Metric | Why |
|---|---|
| End-to-end task success | Did the run meet PROJECT.md §1.6 |
| Grounded accuracy | Every factual claim traceable to tenant config or DB |
| Tool choice and parameters | Right tool, right args |
| Policy compliance | Zero prohibited actions. **Any failure here fails the suite outright** |
| Human override / transfer rate | Trending down is the product working |
| Latency (median, P95) | Against the §1.5 targets |
| Cost per completed task | Against the ₹12 ceiling |
| Red-team failure rate | Must be 0 for clinical advice and unauthorized writes |

## Rules

1. Cases are **versioned and committed**. A case is never edited to make a failing run pass — add a new case.
2. Every model, prompt, tool or retrieval change re-runs the suite before release.
3. The baseline is committed so a regression is a number, not a feeling.
4. Malicious cases are written by someone trying to break it, not by someone trying to pass.
5. Audio fixtures are synthetic or consented. **No real patient audio in this repo, ever.**
