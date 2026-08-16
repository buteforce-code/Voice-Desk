"""Eval harness.

Two modes, and the first one works today:

    python -m evals.run --validate
        Load every case, check it against schema.py, check IDs are unique and
        match their directory, check the malicious slice actually asserts
        violations. Exit non-zero on any problem.

    python -m evals.run --baseline evals/baseline/latest.json
        Drive the agent through every case and score against the baseline.
        Blocked until the pipeline exists; scoring shape is defined here so the
        cases being written now are already scoreable.

`--validate` is what makes parallel case authoring safe: six authors write
independently, and this decides whether what came back is conformant.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from pydantic import ValidationError

from evals.schema import CaseClass, EvalCase, Violation

CASES_DIR = Path(__file__).parent / "cases"


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

    by_class: dict[CaseClass, list[EvalCase]] = defaultdict(list)
    for c in cases:
        by_class[c.case_class].append(c)

    # Every class must exist. An empty slice is a silent coverage hole, and
    # the malicious slice is the one most likely to be quietly skipped.
    for cls in CaseClass:
        if not by_class.get(cls):
            problems.append(f"no cases in class '{cls.value}' — coverage hole")

    # A malicious case that asserts nothing is decoration.
    for c in by_class.get(CaseClass.MALICIOUS, []):
        if not c.expect.must_not:
            problems.append(
                f"{c.id}: malicious case asserts no violations in must_not"
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


def cmd_baseline(path: str) -> int:
    cases, errors = load_cases()
    if errors:
        print("Cases do not validate; fix those before scoring.", file=sys.stderr)
        return cmd_validate()

    baseline_path = Path(path)
    if not baseline_path.exists():
        print(
            f"No baseline at {path}.\n"
            f"Expected — a baseline is committed only once the pipeline can "
            f"produce one. {len(cases)} cases are ready and conformant.",
            file=sys.stderr,
        )
        return 0

    _ = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(
        "Scoring requires the pipeline (G5 completion). "
        f"{len(cases)} cases loaded and conformant.",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="evals.run")
    ap.add_argument("--validate", action="store_true", help="validate cases only")
    ap.add_argument("--baseline", metavar="PATH", help="score against a baseline")
    args = ap.parse_args()

    if args.baseline:
        return cmd_baseline(args.baseline)
    return cmd_validate()


if __name__ == "__main__":
    raise SystemExit(main())
