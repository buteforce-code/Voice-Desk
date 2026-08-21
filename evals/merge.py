"""Fold repeated runs of one case into a single result.

The first two baselines disagreed with each other. Same cases, same model, same
prompt, and **11 of 58 verdicts flipped while 23 of 58 outcomes changed**. The
model is not deterministic at temperature 0.2 -- the tool-calling path diverges,
and one different tool call early sends the rest of the call somewhere else.

A baseline measured once therefore carries about 19% verdict noise, and
`--against` would report roughly eleven regressions and fixes on every run that
are nothing but that noise. A gate that cries wolf eleven times a run is a gate
people stop reading, which is worse than no gate: it converts an absent control
into an ignored one.

So a case is driven N times and the results are folded here. The rules are
asymmetric on purpose, and all three lean the same way -- toward reporting the
worst thing that happened rather than the average of what happened:

  * **A case passes only if it passed every time.** A case that gives medical
    advice one run in three is not a passing case, and the majority verdict
    would call it passing.
  * **Violations union.** A violation seen once is a violation the agent is
    capable of. Averaging it away is exactly the reasoning that produces a
    green suite over a product that occasionally books the wrong patient.
  * **Faults must have fired in every run**, or the case is void. A fault that
    fires two runs in three means one run tested something else.

Rates -- grounding, language -- are averaged instead, because those are already
rates over many decisions and their noise is not the verdict's noise.
"""

from __future__ import annotations

import statistics

from evals.schema import CaseResult


def merge(results: list[CaseResult]) -> CaseResult:
    """One result per case, whatever N was.

    A single-element list comes back with `runs=1` and its own verdict, so the
    default path is unchanged and costs nothing.
    """
    if not results:  # pragma: no cover - the caller always has at least one
        raise ValueError("nothing to merge")
    if len(results) == 1:
        return results[0]

    first = results[0]
    passes = sum(1 for r in results if r.passed)
    violations = sorted({v for r in results for v in r.violations}, key=lambda v: v.value)

    return first.model_copy(
        update={
            "runs": len(results),
            "passes": passes,
            # Strict: every run, or the case did not pass.
            "task_success": all(r.task_success for r in results),
            "violations": violations,
            "faults_injected_ok": all(r.faults_injected_ok for r in results),
            "not_run": all(r.not_run for r in results),
            "outcome_actual": _modal_outcome(results),
            "grounded_accuracy": _mean(r.grounded_accuracy for r in results),
            "claims_checked": sum(r.claims_checked for r in results),
            "claims_unverifiable": sum(r.claims_unverifiable for r in results),
            "tool_choice_correct": all(r.tool_choice_correct for r in results),
            "language_turns_correct": sum(r.language_turns_correct for r in results),
            "language_turns_total": sum(r.language_turns_total for r in results),
            "transferred": any(r.transferred for r in results),
            "turns_used": int(statistics.median(r.turns_used for r in results)),
            "latency_median_ms": int(statistics.median(r.latency_median_ms for r in results)),
            "latency_p95_ms": max(r.latency_p95_ms for r in results),
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "throttled": sum(r.throttled for r in results),
            "notes": _notes(results, passes),
        }
    )


def _modal_outcome(results: list[CaseResult]) -> object:
    """The outcome the case reached most often.

    Reported rather than scored -- `task_success` above is already strict. It
    is here so a reader can see what the call usually did, which is the first
    question after "why did this fail".
    """
    counts: dict[object, int] = {}
    for result in results:
        counts[result.outcome_actual] = counts.get(result.outcome_actual, 0) + 1
    return max(counts, key=lambda outcome: counts[outcome])


def _notes(results: list[CaseResult], passes: int) -> str | None:
    """Keep the flake visible, then the first run's detail.

    A case at 2/3 is where the next fix should go and a case at 0/3 is a
    different problem, so the ratio leads. Detail from every run would be
    unreadable at 58 cases; the outcomes it reached are the part that differs.
    """
    lines = []
    if passes and passes < len(results):
        outcomes = ", ".join(sorted({r.outcome_actual.value for r in results}))
        lines.append(f"FLAKY {passes}/{len(results)} — reached: {outcomes}")

    # A VOID note outranks the others. Taking the first run's notes hid one:
    # `ambiguous-002` crashed in one of its three runs and reported the note
    # from run 1, so the case was marked void with nothing on the row to say
    # why. Whatever made a run unscoreable is the first thing a reader needs.
    voided = next((r.notes for r in results if r.notes and "VOID" in r.notes), None)
    if voided:
        lines.append(voided)
    else:
        for result in results:
            if result.notes:
                lines.append(result.notes)
                break
    return " | ".join(lines)[:2000] or None


def _mean(values: object) -> float:
    collected = list(values)  # type: ignore[call-overload]
    return round(sum(collected) / len(collected), 4) if collected else 0.0
