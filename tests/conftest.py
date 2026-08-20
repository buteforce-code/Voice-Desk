"""Shared fixtures.

Everything here runs without a database, a model, or a telephony account. That
is deliberate: the tests that assert the prohibited row of the risk register
must be runnable in CI on a bare checkout, or they will be skipped exactly when
they matter.
"""

from __future__ import annotations

import re
import token
import tokenize
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools
from voicedesk.tools.schemas import AppointmentOut, SlotOut, ToolContext

REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "src"
MIGRATIONS = REPO_ROOT / "db" / "migrations"

CLINIC_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLINIC_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

# Fictional numbers in the valid Indian mobile range. VERIFIED_MSISDN is the
# caller who passed the DOB challenge; OTHER_MSISDN is anybody else.
# The target prospect is a real organisation that has never been contacted.
# D5: it is named only in the private vault note, never in this repo. Defined
# once here so the tests that enforce that share one list -- and so the scan
# can tell a file participating in the guard from a file leaking the name.
FORBIDDEN_ORG_TOKENS = ("sitapati", "royapettah")

VERIFIED_MSISDN = "+919876543210"
OTHER_MSISDN = "+919812345678"


# --------------------------------------------------------------------------
# Source access — several tests assert on files rather than behaviour, because
# "this code path does not exist" cannot be asserted by calling it.
# --------------------------------------------------------------------------


def source_files(suffixes: tuple[str, ...] = (".py",)) -> list[Path]:
    return [
        p
        for p in SRC.rglob("*")
        if p.suffix in suffixes and "__pycache__" not in p.parts
    ]


def migration_sql() -> str:
    """Every migration concatenated, comments stripped.

    Comments must go before any structural assertion runs. These migrations
    explain at length what they deliberately do *not* grant — "no DELETE grant",
    "no diagnosis, symptom or prescription column" — so a naive search for
    `delete` or `diagnosis` matches the very prose promising their absence. The
    first version of this suite failed on four such self-inflicted hits.

    Where a test genuinely needs the prose (the FORCE RLS rationale), it reads
    the file directly instead.
    """
    raw = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql"))
    )
    return strip_sql_comments(raw)


def strip_sql_comments(sql: str) -> str:
    without_line_comments = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.DOTALL)


def code_identifiers(path: Path) -> set[str]:
    """Names and attribute accesses that appear as *code* in a Python file.

    String literals and comments are excluded. Without that distinction, a test
    asserting "no outbound-call code path exists" fires on
    `_PROHIBITED_BY_ABSENCE`, the frozenset that names the forbidden calls so
    they can be checked for — a guard tripping over its own guard list, which is
    the kind of false positive that gets a blocking CI job disabled.
    """
    names: set[str] = set()
    with path.open("rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == token.NAME:
                    names.add(tok.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
            return names
    return names


def called_names(path: Path) -> set[str]:
    """Identifiers that appear immediately before a `(` — i.e. actually called."""
    called: set[str] = set()
    previous: str | None = None
    with path.open("rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == token.OP and tok.string == "(" and previous:
                    called.add(previous)
                if tok.type == token.NAME:
                    previous = tok.string
                elif tok.type != token.OP or tok.string != ".":
                    previous = None
        except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
            return called
    return called


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class RecordingAudit:
    """In-memory AuditSink. Keeps every attempt so tests can assert that a
    rejection was recorded, not just that it was refused."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.replays: dict[str, dict[str, Any]] = {}

    async def record(
        self,
        ctx: ToolContext,
        tool_name: str,
        args_redacted: dict[str, Any],
        result: str,
        idempotency_key: str,
        authorized_by: str,
        rejection_reason: str | None = None,
    ) -> None:
        self.rows.append(
            {
                "clinic_id": ctx.clinic_id,
                "tool_name": tool_name,
                "args_redacted": args_redacted,
                "result": result,
                "idempotency_key": idempotency_key,
                "authorized_by": authorized_by,
                "rejection_reason": rejection_reason,
            }
        )

    async def find_replay(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.replays.get(idempotency_key)

    # -- assertions helpers ------------------------------------------------

    def results_for(self, tool_name: str) -> list[str]:
        return [r["result"] for r in self.rows if r["tool_name"] == tool_name]


class StubAdapter:
    """Satisfies SchedulingAdapter without touching Postgres.

    Returns fixed, obviously-fake data. No test in this suite asserts on the
    values — they assert on whether the call was permitted at all.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def find_slots(self, clinic_id: UUID, **kwargs: Any) -> list[SlotOut]:
        self.calls.append("find_slots")
        return []

    async def find_appointments(
        self, clinic_id: UUID, **kwargs: Any
    ) -> list[AppointmentOut]:
        self.calls.append("find_appointments")
        return []

    async def hold_slot(
        self, clinic_id: UUID, slot_id: UUID, call_id: UUID, ttl_seconds: int
    ) -> datetime:
        self.calls.append("hold_slot")
        return datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    async def release_hold(self, clinic_id: UUID, call_id: UUID) -> None:
        self.calls.append("release_hold")

    async def confirm_booking(
        self, clinic_id: UUID, **kwargs: Any
    ) -> tuple[UUID, datetime, str]:
        self.calls.append("confirm_booking")
        return uuid4(), datetime.now(UTC) + timedelta(days=1), "Dr Fictional"

    async def reschedule(self, clinic_id: UUID, **kwargs: Any) -> tuple[UUID, datetime]:
        self.calls.append("reschedule")
        return uuid4(), datetime.now(UTC) + timedelta(days=2)

    async def cancel(self, clinic_id: UUID, **kwargs: Any) -> datetime:
        self.calls.append("cancel")
        return datetime.now(UTC)

    async def undo(self, clinic_id: UUID, **kwargs: Any) -> None:
        self.calls.append("undo")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_developer_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `Settings.load()` reading the repo's own `.env`.

    The moment .env loading was added, every config test became dependent on
    whether the machine running it happened to have a real key on disk --
    green on CI, red locally, for reasons nothing in the test says. The tests
    that are specifically ABOUT dotenv pass an explicit `env_file` and are
    unaffected.
    """
    import voicedesk.config as config

    monkeypatch.setattr(
        config,
        "load_dotenv_once",
        lambda path=None: None if path is None else _real_dotenv(path),
    )


def _real_dotenv(path) -> None:
    from dotenv import load_dotenv

    if path is not None and path.is_file():
        load_dotenv(path, override=False)


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def adapter() -> StubAdapter:
    return StubAdapter()


@pytest.fixture
def config() -> TenantConfig:
    # Fictional tenant. PROJECT.md D5: no real clinic's details appear anywhere,
    # including in tests.
    return TenantConfig(
        {
            "opd_hours": "Mon-Sat 09:00-13:00, 17:00-20:00",
            "address": "12 Fictional Street, Meridian",
            "consult_fee": "500",
            "specialties": "general medicine, paediatrics",
            "doctors": "Dr Fictional, Dr Placeholder",
            "prep_instructions": "Bring any previous prescriptions.",
            "languages": "Tamil, Hindi, English",
        },
        escalation_msisdn="+919000000000",
    )


@pytest.fixture
def registry(
    audit: RecordingAudit, adapter: StubAdapter, config: TenantConfig
) -> ToolRegistry:
    reg = ToolRegistry(audit)
    register_scheduling_tools(reg, adapter, config)  # type: ignore[arg-type]
    return reg


def make_ctx(
    *,
    clinic_id: UUID = CLINIC_A,
    state: str = "research",
    approval_token: str | None = None,
    dry_run: bool = True,
    speculative: bool = False,
    trace_id: str | None = None,
    identity_verified: bool = False,
    verified_msisdn: str | None = None,
) -> ToolContext:
    """Build a server-side context.

    Note what a test cannot do here either: there is no way to hand the model
    this object. It is constructed by the session, which is the entire point of
    the design being tested.
    """
    return ToolContext(
        clinic_id=clinic_id,
        call_id=uuid4(),
        trace_id=trace_id or f"trace-{uuid4()}",
        state=state,
        dry_run=dry_run,
        approval_token=approval_token,
        speculative=speculative,
        identity_verified=identity_verified,
        verified_msisdn=verified_msisdn,
    )


def verified_ctx(**kwargs: Any) -> ToolContext:
    """A context that has passed the DOB challenge.

    Used where a test means to exercise some OTHER gate. Without it the
    identity check fires first and masks whatever was actually under test --
    which is correct behaviour and useless as a test.
    """
    kwargs.setdefault("identity_verified", True)
    kwargs.setdefault("verified_msisdn", VERIFIED_MSISDN)
    return make_ctx(**kwargs)
