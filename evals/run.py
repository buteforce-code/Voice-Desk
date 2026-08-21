"""Eval harness.

    python -m evals.run --validate
        Load every case, check it against schema.py, check IDs are unique and
        match their directory, check the malicious slice actually asserts
        violations. No model, no key, no network. Runs in CI.

    python -m evals.run --run [--class malicious] [--case normal-001] [--limit N]
        Drive the agent through the cases and print a scored report. Real
        model, real tool calling, real state machine. Costs money and time.

    python -m evals.run --run --write-baseline evals/baseline/latest.json
        Same, and commit the result as the baseline.

    python -m evals.run --against evals/baseline/latest.json
        Run and diff against a committed baseline. Exits non-zero on a
        per-case regression, which is what makes it usable as a gate.

`--validate` is what makes parallel case authoring safe: six authors write
independently, and this decides whether what came back is conformant. It stays
separate from `--run` because it must be runnable on a bare checkout -- a check
that needs an API key is a check that gets skipped exactly when it matters.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from pydantic import ValidationError

from evals.schema import CaseClass, EvalCase, Fault, Violation

CASES_DIR = Path(__file__).parent / "cases"
SCHEDULING_SRC = Path(__file__).parent.parent / "src" / "voicedesk" / "tools" / "scheduling.py"


def registered_tools() -> set[str]:
    """Parse the live tool names out of the registry source.

    Read from source rather than imported so --validate stays runnable without
    pipecat, asyncpg and the rest of the runtime installed. If the parse ever
    returns nothing, that is a hard failure -- silently validating against an
    empty set would defeat the whole check.
    """
    src = SCHEDULING_SRC.read_text(encoding="utf-8")
    names = set(re.findall(r'registry\.register\(\s*\n?\s*"([a-z_]+)"', src))
    if not names:
        raise RuntimeError(f"parsed no tool names from {SCHEDULING_SRC}")
    return names


def load_cases() -> tuple[list[EvalCase], list[str]]:
    """Returns (cases, errors). Never raises on a bad case — collects instead,
    so one malformed file does not hide the other twenty."""
    cases: list[EvalCase] = []
    errors: list[str] = []

    for path in sorted(CASES_DIR.rglob("*.yaml")):
        rel = path.relative_to(CASES_DIR)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: unparseable YAML — {exc}")
            continue

        if raw is None:
            errors.append(f"{rel}: empty file")
            continue

        try:
            case = EvalCase.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(f"{rel}: {loc} — {err['msg']}")
            continue

        # The directory is the authority on case class. A case filed under
        # malicious/ that claims to be normal is a filing error.
        folder = rel.parts[0]
        if case.case_class.value != folder:
            errors.append(
                f"{rel}: case_class '{case.case_class.value}' "
                f"does not match directory '{folder}'"
            )

        cases.append(case)

    return cases, errors


def check_invariants(cases: list[EvalCase]) -> list[str]:
    """Rules that hold across the whole set, not within one case."""
    problems: list[str] = []

    seen: dict[str, str] = {}
    for c in cases:
        if c.id in seen:
            problems.append(f"duplicate case id '{c.id}'")
        seen[c.id] = c.description

    # Tool names are free strings in the schema, so a typo or an invented tool
    # would otherwise score as a silent non-result forever. Six such references
    # shipped in the first batch before this check existed.
    tools = registered_tools()
    for c in cases:
        for field in ("tools_expected", "tools_forbidden"):
            for name in getattr(c.expect, field):
                if name not in tools:
                    problems.append(
                        f"{c.id}: {field} references unknown tool '{name}' "
                        f"(registered: {', '.join(sorted(tools))})"
                    )

    # A transfer outcome without a reason cannot be scored against the
    # uncertainty triggers in docs/STATE_MACHINE.md.
    for c in cases:
        if c.expect.outcome.value == "transferred" and c.expect.transfer_reason is None:
            problems.append(f"{c.id}: outcome 'transferred' with no transfer_reason")

    # An effort floor above the ceiling is unsatisfiable.
    for c in cases:
        if c.expect.min_agent_turns > c.expect.max_total_turns:
            problems.append(
                f"{c.id}: min_agent_turns {c.expect.min_agent_turns} exceeds "
                f"max_total_turns {c.expect.max_total_turns}"
            )

    by_class: dict[CaseClass, list[EvalCase]] = defaultdict(list)
    for c in cases:
        by_class[c.case_class].append(c)

    # Every class must exist. An empty slice is a silent coverage hole, and
    # the malicious slice is the one most likely to be quietly skipped.
    for cls in CaseClass:
        if not by_class.get(cls):
            problems.append(f"no cases in class '{cls.value}' — coverage hole")

    # A malicious case that names no target is decoration.
    for c in by_class.get(CaseClass.MALICIOUS, []):
        if not c.expect.probes:
            problems.append(f"{c.id}: malicious case probes no violations")

    # Every violation must be probed somewhere. An unprobed violation means a
    # detector nothing exercises -- which is how a broken detector reports a
    # clean suite. This is what `probes` is FOR, now that it is honest
    # metadata rather than a fake assertion.
    probed = {v for c in cases for v in c.expect.probes}
    for v in Violation:
        if v not in probed:
            problems.append(f"no case probes violation '{v.value}' — detector untested")

    # Every fault must be either exercised by a case or declared
    # unimplemented. Same argument as the violation check above: a fault the
    # harness can produce and nothing triggers is untested harness code that
    # reads as coverage. Declaring the omission is cheap; discovering it when a
    # case silently stops testing anything is not.
    declared = {f for c in cases for f in c.inject}
    for fault in Fault:
        if fault not in declared and fault not in _unimplemented_faults():
            problems.append(
                f"fault '{fault.value}' is neither injected by any case nor listed "
                f"in evals.faults.UNIMPLEMENTED — implement it or declare it absent"
            )

    # A code-switch case that never switches language is mislabelled.
    for c in by_class.get(CaseClass.CODESWITCH, []):
        langs = {t.language for t in c.turns if t.language}
        if len(langs) < 2 and not any(
            t.say and _looks_mixed(t.say) for t in c.turns
        ):
            problems.append(
                f"{c.id}: codeswitch case shows no language mixing"
            )

    return problems


def _unimplemented_faults() -> frozenset[Fault]:
    """Imported lazily so `--validate` keeps working without the runtime.

    `evals.faults` imports the adapters, which import the tool schemas. That is
    fine in a synced environment and not fine on the bare checkout this command
    is supposed to run on, so a failure to import is treated as an empty set --
    which makes the check stricter, never looser.
    """
    try:
        from evals.faults import UNIMPLEMENTED
    except ImportError:  # pragma: no cover - bare checkout
        return frozenset()
    return UNIMPLEMENTED


def _looks_mixed(text: str) -> bool:
    """Crude: does the utterance contain both Latin and an Indic script?"""
    has_latin = any("a" <= ch.lower() <= "z" for ch in text)
    has_indic = any(
        "ऀ" <= ch <= "ॿ" or "஀" <= ch <= "௿" for ch in text
    )
    return has_latin and has_indic


def cmd_validate() -> int:
    cases, errors = load_cases()
    errors += check_invariants(cases)

    by_class: dict[str, int] = defaultdict(int)
    for c in cases:
        by_class[c.case_class.value] += 1

    print("=" * 62)
    print(" Voice Desk — eval case validation")
    print("=" * 62)
    for cls in CaseClass:
        n = by_class.get(cls.value, 0)
        mark = " " if n else "!"
        print(f" {mark} {cls.value:<12} {n:>3} cases")
    print("-" * 62)
    print(f" {len(cases)} cases total")

    if errors:
        print(f"\n {len(errors)} problem(s):\n")
        for e in errors:
            print(f"   FAIL  {e}")
        return 1

    print(" All cases conformant.")
    return 0


async def cmd_run(
    *,
    only_class: str | None,
    only_case: str | None,
    limit: int | None,
    write_baseline: str | None,
    against: str | None,
    version: str,
    concurrency: int = 4,
    repeats: int = 1,
) -> int:
    """Score the suite against a live model.

    Imported lazily and locally: `--validate` must keep working on a checkout
    with neither the runtime nor a key installed, and a module-level import of
    the driver would take that away.
    """
    from evals.report import build_baseline, diff, load_baseline, print_results
    from evals.report import write_baseline as commit
    from voicedesk.config import ConfigError, Settings
    from voicedesk.llm import build_from_settings

    cases, errors = load_cases()
    if errors:
        print("Cases do not validate; fix those before scoring.", file=sys.stderr)
        return cmd_validate()

    cases = _select(cases, only_class=only_class, only_case=only_case, limit=limit)
    if not cases:
        print("No cases matched the selection.", file=sys.stderr)
        return 1

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuration refused: {exc}", file=sys.stderr)
        return 2

    model = build_from_settings(settings)
    if model is None:
        print(
            "No model configured. Set GOOGLE_AI_API_KEY or OPENROUTER_API_KEY in .env.\n"
            "A baseline produced against a stubbed model would be a number about the stub.",
            file=sys.stderr,
        )
        return 2

    results = await _drive(cases, model, settings.llm_model, concurrency, repeats)

    expected = {c.id: c.expect.outcome for c in cases}
    baseline = build_baseline(
        results,
        expected,
        version=version,
        prompt_version="prompt-2026-08-21",
        model_version=settings.llm_model,
        concurrency=concurrency,
        repeats=repeats,
    )
    print_results(results, baseline)

    exit_code = 0
    if against:
        path = Path(against)
        # Blocking file IO in an async function. This is a CLI reading one
        # small JSON file once, not a server: the alternative is an async
        # filesystem dependency the project does not otherwise need.
        if not path.exists():  # noqa: ASYNC240
            print(f"No baseline at {against} to compare against.", file=sys.stderr)
            return 1
        previous, prior_cases = load_baseline(path)
        exit_code = diff(previous, prior_cases, results, repeats)

    if write_baseline:
        target = Path(write_baseline)
        commit(baseline, results, target)
        print(f"\n baseline written to {target}")

    return exit_code


async def _drive(
    cases: list[EvalCase],
    model: object,
    model_version: str,
    concurrency: int,
    repeats: int = 1,
) -> list[object]:
    """Run cases concurrently, bounded, `repeats` times each.

    Concurrency is safe because each case gets its own world -- tenant,
    calendar, registry, audit log, session -- and shares nothing but the model
    client. That isolation was built for correctness (a reused adapter makes the
    second case double-book) and it buys this for free.

    It is also the difference between a suite that runs on every change and one
    that does not. Sequentially, 58 cases against a 6s-per-turn model is over
    two hours; nobody runs that before a commit, and a gate nobody runs is not
    a gate.

    Bounded rather than unbounded: providers rate-limit, and a 429 storm turns
    every case into a crash-void, which reads as a catastrophic regression.

    `repeats` is why this exists at all rather than being a for-loop. The first
    two baselines disagreed on 11 of 58 verdicts with nothing changed between
    them, so one run per case is a snapshot and not a measurement. Repeats of
    the SAME case run concurrently with each other, so raising it costs wall
    clock roughly in proportion to how far it exceeds the concurrency limit.
    """
    from evals.driver import run_case
    from evals.merge import merge
    from evals.score import score

    gate = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(cases) * repeats

    async def one(case: EvalCase) -> object:
        nonlocal done
        async with gate:
            record = await run_case(case, model, model_version=model_version)  # type: ignore[arg-type]
            result = score(case, record)
        done += 1
        print(f"  [{done}/{total}] {case.id}", file=sys.stderr, flush=True)
        return result

    async def repeated(case: EvalCase) -> object:
        runs = await asyncio.gather(*(one(case) for _ in range(repeats)))
        return merge(list(runs))  # type: ignore[arg-type]

    # Results come back in CASE order regardless of completion order, so two
    # printed reports are line-comparable.
    return list(await asyncio.gather(*(repeated(c) for c in cases)))


def _select(
    cases: list[EvalCase],
    *,
    only_class: str | None,
    only_case: str | None,
    limit: int | None,
) -> list[EvalCase]:
    selected = sorted(cases, key=lambda c: c.id)
    if only_class:
        selected = [c for c in selected if c.case_class.value == only_class]
    if only_case:
        selected = [c for c in selected if c.id == only_case]
    if limit:
        selected = selected[:limit]
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(prog="evals.run")
    ap.add_argument("--validate", action="store_true", help="validate cases only")
    ap.add_argument("--run", action="store_true", help="drive the agent and score")
    ap.add_argument("--class", dest="only_class", metavar="CLASS", help="one case class")
    ap.add_argument("--case", dest="only_case", metavar="ID", help="one case id")
    ap.add_argument("--limit", type=int, metavar="N", help="first N cases")
    ap.add_argument("--write-baseline", metavar="PATH", help="commit the result")
    ap.add_argument("--against", metavar="PATH", help="diff against a committed baseline")
    ap.add_argument("--version", default="v1", help="baseline version label")
    ap.add_argument(
        "--concurrency", type=int, default=4, metavar="N", help="cases in flight (default 4)"
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="drive each case N times; a case passes only if it passes every time",
    )
    args = ap.parse_args()

    if args.run or args.write_baseline or args.against:
        return asyncio.run(
            cmd_run(
                only_class=args.only_class,
                only_case=args.only_case,
                limit=args.limit,
                write_baseline=args.write_baseline,
                against=args.against,
                version=args.version,
                concurrency=args.concurrency,
                repeats=args.repeat,
            )
        )
    return cmd_validate()


if __name__ == "__main__":
    raise SystemExit(main())
