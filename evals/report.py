"""Aggregate scored cases into a baseline, and print what happened.

The metrics in PROJECT.md 1.5 are written about production traffic. Two of them
do not survive a naive translation to a 58-case suite, and translating them
naively is how a baseline ends up meaning something other than what it says:

  * **Resolution rate** -- 16 of the 58 cases are *supposed* to end in a
    transfer. Measured across the whole suite, an agent that transferred every
    single call would score 28% on a metric whose target is 70%, and an agent
    that never transferred would score higher while failing every red-team
    case. It is measured here over the cases whose correct ending is a
    resolution, and nowhere else.

  * **Booking accuracy** -- only meaningful over cases that were supposed to
    book. A suite-wide denominator dilutes it with cases where booking would
    itself have been the failure.

Both denominators are named in the printed report, because a rate whose
denominator is a guess is not a measurement.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from evals.schema import Baseline, CaseClass, CaseResult, Outcome

RESOLVING = {Outcome.BOOKED, Outcome.RESCHEDULED, Outcome.CANCELLED, Outcome.FAQ_ANSWERED}


def build_baseline(
    results: list[CaseResult],
    expected: dict[str, Outcome],
    *,
    version: str,
    prompt_version: str,
    model_version: str,
    concurrency: int = 1,
    repeats: int = 1,
) -> Baseline:
    scored = [r for r in results if not r.not_run]

    resolving_ids = {cid for cid, out in expected.items() if out in RESOLVING}
    should_resolve = [r for r in scored if r.case_id in resolving_ids]
    resolved = [r for r in should_resolve if r.outcome_actual in RESOLVING]

    should_book = [r for r in scored if expected.get(r.case_id) is Outcome.BOOKED]
    booked_right = [r for r in should_book if r.task_success]

    latencies = [r.latency_median_ms for r in scored if r.latency_median_ms]
    p95s = [r.latency_p95_ms for r in scored if r.latency_p95_ms]

    lang_correct = sum(r.language_turns_correct for r in scored)
    lang_total = sum(r.language_turns_total for r in scored)

    claims = sum(r.claims_checked for r in scored)
    grounded_micro = sum(round(r.grounded_accuracy * r.claims_checked) for r in scored)
    with_claims = [r for r in scored if r.claims_checked]

    return Baseline(
        version=version,
        committed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        prompt_version=prompt_version,
        model_version=model_version,
        total=len(results),
        passed=sum(1 for r in results if r.passed),
        voided=sum(1 for r in results if not r.faults_injected_ok and not r.not_run),
        not_run=sum(1 for r in results if r.not_run),
        by_class=_by_class(results),
        resolution_rate=_ratio(len(resolved), len(should_resolve)),
        booking_accuracy=_ratio(len(booked_right), len(should_book)),
        latency_median_ms=int(statistics.median(latencies)) if latencies else 0,
        latency_p95_ms=max(p95s) if p95s else 0,
        concurrency=concurrency,
        repeats=repeats,
        language_accuracy=_ratio(lang_correct, lang_total),
        red_team_failures=sum(
            1 for r in scored if r.case_class is CaseClass.MALICIOUS and not r.passed
        ),
        prompt_tokens_total=sum(r.prompt_tokens for r in results),
        completion_tokens_total=sum(r.completion_tokens for r in results),
        cost_per_booking_inr=None,
        # MACRO: each case gets one vote.
        #
        # Claim-weighted, one degenerate call swamps the suite. A repetition
        # loop in a single run of `edge-001` produced close to a thousand
        # checkable claims and carried 2990 of the suite's 3489 -- so the
        # headline number was a report on one broken call with 57 cases as
        # rounding error. The suite's unit is the case, and so is this.
        grounded_accuracy=(
            round(sum(r.grounded_accuracy for r in with_claims) / len(with_claims), 4)
            if with_claims
            else 1.0
        ),
        grounded_accuracy_by_claim=_ratio(grounded_micro, claims),
        claims_checked=claims,
        claims_unverifiable=sum(r.claims_unverifiable for r in scored),
        throttled=sum(r.throttled for r in results),
    )


def _by_class(results: list[CaseResult]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for case_class in CaseClass:
        slice_ = [r for r in results if r.case_class is case_class]
        table[case_class.value] = {
            "total": len(slice_),
            "passed": sum(1 for r in slice_ if r.passed),
            "voided": sum(1 for r in slice_ if not r.faults_injected_ok and not r.not_run),
            "not_run": sum(1 for r in slice_ if r.not_run),
        }
    return table


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


# -- printing ---------------------------------------------------------------

RULE = "=" * 74


def print_results(results: list[CaseResult], baseline: Baseline) -> None:
    print(RULE)
    print(" Voice Desk — eval run")
    print(RULE)

    for result in sorted(results, key=lambda r: r.case_id):
        print(f" {_mark(result)}  {result.case_id:<16} {result.outcome_actual.value:<14}"
              f" grounded {result.grounded_accuracy:.2f}/{result.claims_checked:<3}"
              f" {_flags(result)}")
        if result.notes:
            for chunk in result.notes.split(" | "):
                print(f"        {chunk[:110]}")

    print("-" * 74)
    for name, row in baseline.by_class.items():
        extra = f" not-run {row['not_run']}" if row.get("not_run") else ""
        print(f" {name:<12} {row['passed']:>2}/{row['total']:<3} passed"
              f"  voided {row['voided']}{extra}")

    print("-" * 74)
    print(f" passed              {baseline.passed}/{baseline.total}"
          f"   ({baseline.repeats} run(s) per case, all must pass)")
    if baseline.repeats > 1:
        flaky = [r for r in results if r.passes and not r.passed]
        if flaky:
            print(f" flaky               {len(flaky)}   "
                  f"passed some runs and not others: "
                  f"{', '.join(sorted(r.case_id for r in flaky))}")
    print(f" voided              {baseline.voided}")
    print(f" not run             {baseline.not_run}")
    print(f" resolution rate     {baseline.resolution_rate:.1%}   "
          f"(cases whose correct ending is a resolution)")
    print(f" booking accuracy    {baseline.booking_accuracy:.1%}   "
          f"(cases whose correct ending is a booking)")
    print(f" grounded accuracy   {baseline.grounded_accuracy:.1%}   "
          f"mean over cases that made a claim")
    print(f"   by claim          {baseline.grounded_accuracy_by_claim:.1%}   "
          f"over {baseline.claims_checked} claims "
          f"({baseline.claims_unverifiable} seen but not checkable)")
    print(f" language accuracy   {baseline.language_accuracy:.1%}   "
          f"turn-level")
    caveat = (
        ""
        if baseline.concurrency <= 1
        else f"   NOT A MEASUREMENT — {baseline.concurrency} cases in flight"
    )
    print(
        f" latency median/p95  {baseline.latency_median_ms} / "
        f"{baseline.latency_p95_ms} ms{caveat}"
    )
    print(f" red-team failures   {baseline.red_team_failures}")
    print(f" tokens in/out       {baseline.prompt_tokens_total} / "
          f"{baseline.completion_tokens_total}   (cost: no price table yet — G7)")
    if baseline.throttled:
        print(f" provider throttles  {baseline.throttled}   "
              f"retried by the harness, not scored against the agent")
    print(RULE)


def _mark(result: CaseResult) -> str:
    if result.not_run:
        return "SKIP"
    if not result.faults_injected_ok:
        return "VOID"
    if result.passed:
        return "PASS"
    # A case that passed some of its runs is a different problem from one that
    # never passes, and collapsing both to FAIL hides where the next fix goes.
    return "FLAK" if result.passes else "FAIL"


def _flags(result: CaseResult) -> str:
    bits = []
    if result.violations:
        bits.append("!" + ",".join(v.value for v in result.violations))
    if not result.tool_choice_correct:
        bits.append("tools")
    if not result.task_success and result.faults_injected_ok and not result.not_run:
        bits.append("task")
    return " ".join(bits)


def write_baseline(baseline: Baseline, results: list[CaseResult], path: Path) -> None:
    """The baseline and the runs that produced it, in one file.

    Per-case results are stored alongside the aggregate deliberately: a
    regression shows up as a number, and the next question is always which case
    moved. A baseline that cannot answer that is a number nobody can act on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "baseline": baseline.model_dump(mode="json"),
        "cases": [r.model_dump(mode="json") for r in sorted(results, key=lambda r: r.case_id)],
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> tuple[Baseline, dict[str, CaseResult]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    baseline = Baseline.model_validate(document["baseline"])
    cases = {c["case_id"]: CaseResult.model_validate(c) for c in document.get("cases", [])}
    return baseline, cases


def diff(
    previous: Baseline,
    prior_cases: dict[str, CaseResult],
    results: list[CaseResult],
    repeats: int = 1,
) -> int:
    """Compare a run against a committed baseline. Returns an exit code.

    Refuses across schema revisions rather than printing a misleading delta --
    r2's `grounded_accuracy` and r3's are computed differently and the numbers
    are not comparable, which is the entire reason `schema_revision` exists.
    """
    if previous.repeats != repeats:
        print(
            f"Baseline was measured at {previous.repeats} run(s) per case and this "
            f"run used {repeats}. Not comparable — a stricter or looser pass rule "
            f"moves cases for reasons that are not regressions."
        )
        return 1

    if previous.schema_revision != Baseline.model_fields["schema_revision"].default:
        print(
            f"Baseline is schema revision {previous.schema_revision}; this harness "
            f"computes revision {Baseline.model_fields['schema_revision'].default}. "
            f"Not comparable — re-baseline rather than trusting the delta.",
        )
        return 1

    regressions = []
    fixes = []
    for result in results:
        was = prior_cases.get(result.case_id)
        if was is None:
            continue
        if was.passed and not result.passed:
            regressions.append(result.case_id)
        elif not was.passed and result.passed:
            fixes.append(result.case_id)

    print(f"\n baseline {previous.version} — {previous.passed}/{previous.total} passed")
    if fixes:
        print(f" fixed:       {', '.join(sorted(fixes))}")
    if regressions:
        print(f" REGRESSED:   {', '.join(sorted(regressions))}")
    if not fixes and not regressions:
        print(" no per-case movement")
    return 1 if regressions else 0
