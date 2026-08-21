"""One isolated world per eval case.

Every case gets its own tenant snapshot, its own seeded slot calendar, its own
adapter, registry, audit log and session. Nothing is shared between cases, for
a reason that is easy to get wrong: an adapter reused across cases carries the
appointments the previous case booked, and the second case then double-books,
or finds an appointment its caller never made. That is a harness defect that
looks exactly like an agent defect in the results table.

The slot calendar is seeded from a day-floored clock rather than `now()`. Two
runs on the same day produce identical slot times, so a diff between them is
about the agent and not about what o'clock it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.agent import Agent
from voicedesk.audit import InMemoryAudit
from voicedesk.llm import LanguageModel
from voicedesk.state import CallSession, VersionStamp
from voicedesk.tenants import Tenant, load_tenants
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools

REPO = Path(__file__).parent.parent
TENANTS_DIR = REPO / "config" / "tenants"

CALLER_MSISDN = "+919876543210"
"""The ANI every eval call arrives on. Fictional, in the valid Indian mobile
range. Cases that need a *different* caller are about identity, and say so."""


def reference_clock() -> datetime:
    """Midnight UTC today.

    Floored so that two runs on the same day seed the same calendar. Not frozen
    to a literal date, because a hardcoded past date seeds a fortnight of slots
    that have already happened, and `find_slots` filtering on `earliest` would
    then legitimately return nothing for every case in the suite.
    """
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class World:
    """Everything one case needs, and nothing shared with any other case."""

    tenant: Tenant
    adapter: InMemoryAdapter
    audit: InMemoryAudit
    registry: ToolRegistry
    session: CallSession
    agent: Agent
    clock: datetime

    pre_existing: frozenset[UUID] = frozenset()
    """Appointments seeded BEFORE the call, for reschedule and cancel cases.

    Held separately because the scorer must not credit or blame the agent for
    them. Without this the seeded row counts as a booking the call produced:
    it is `confirmed`, `find_slots` never returned its slot, and every
    reschedule and cancel case reports SPECULATIVE_WRITE against an agent that
    did nothing at all."""

    @property
    def confirmed(self) -> list[object]:
        """Everything confirmed in the register, seeded rows included."""
        return [a for a in self.adapter.appointments.values() if a.status == "confirmed"]

    @property
    def created(self) -> list[object]:
        """Confirmed appointments THIS CALL produced.

        The single source of truth for whether a booking happened. What the
        agent *said* happened is scored separately, and disagreement between
        the two is `FABRICATED_SUCCESS`.
        """
        return [a for a in self.confirmed if a.appointment_id not in self.pre_existing]


def build_world(
    *,
    model: LanguageModel,
    tenant_slug: str = "meridian",
    language: str = "en-IN",
    trace_id: str | None = None,
    prompt_version: str = "prompt-2026-08-21",
    model_version: str = "unknown",
    adapter: InMemoryAdapter | None = None,
    clock: datetime | None = None,
) -> World:
    """Assemble one call.

    `adapter` is injectable so a fault-injecting wrapper can be substituted
    without this function knowing faults exist.
    """
    at = clock or reference_clock()
    tenant = load_tenants(TENANTS_DIR)[tenant_slug]
    scheduling = adapter if adapter is not None else InMemoryAdapter.seeded(tenant, start=at)

    audit = InMemoryAudit()
    registry = ToolRegistry(audit)
    register_scheduling_tools(registry, scheduling, TenantConfig.from_tenant(tenant))

    call_id = uuid4()
    session = CallSession(
        clinic_id=tenant.clinic_id,
        call_id=call_id,
        trace_id=trace_id or f"eval-{call_id.hex[:8]}",
        versions=VersionStamp(prompt_version, model_version),
        dry_run=False,
    )
    # The telephony layer carries the ANI in production. Nothing downstream may
    # read it from model output — D12 — so the harness supplies it the same way
    # a real leg would.
    session.ani = CALLER_MSISDN  # type: ignore[attr-defined]

    agent = Agent(
        tenant=tenant,
        session=session,
        registry=registry,
        model=model,
        audit=audit,
        language=language,
    )
    resolved = _unwrap(scheduling)
    return World(
        tenant=tenant,
        adapter=resolved,
        audit=audit,
        registry=registry,
        session=session,
        agent=agent,
        clock=at,
        pre_existing=frozenset(resolved.appointments),
    )


def seeded_adapter(tenant_slug: str = "meridian", clock: datetime | None = None) -> InMemoryAdapter:
    tenant = load_tenants(TENANTS_DIR)[tenant_slug]
    return InMemoryAdapter.seeded(tenant, start=clock or reference_clock())


def seed_existing_booking(
    adapter: InMemoryAdapter,
    *,
    specialty: str = "General Medicine",
    days_ahead: int = 3,
    msisdn: str = CALLER_MSISDN,
) -> UUID | None:
    """Put one confirmed appointment in the register before the call starts.

    Reschedule and cancel cases need something to act on. Without it the agent
    correctly reports "you have no appointments", the case scores as a failed
    reschedule, and the fixture — not the agent — is what failed.

    Returns the appointment id, or None if no slot matched (which the caller
    must treat as a fixture error, not as a result).
    """
    target = adapter.tenant.clinic_id
    wanted = reference_clock() + timedelta(days=days_ahead)
    for slot in sorted(adapter.slots.values(), key=lambda s: s.starts_at):
        if slot.clinic_id != target or slot.specialty != specialty:
            continue
        if slot.starts_at < wanted:
            continue
        appointment_id = uuid4()
        adapter.appointments[appointment_id] = _appointment(adapter, appointment_id, slot, msisdn)
        return appointment_id
    return None


def _appointment(
    adapter: InMemoryAdapter, appointment_id: UUID, slot: object, msisdn: str
) -> object:
    from voicedesk.adapters.memory import _Appointment

    return _Appointment(
        appointment_id=appointment_id,
        clinic_id=adapter.tenant.clinic_id,
        patient_msisdn=msisdn,
        doctor_name=slot.doctor_name,  # type: ignore[attr-defined]
        specialty=slot.specialty,  # type: ignore[attr-defined]
        slot_id=slot.slot_id,  # type: ignore[attr-defined]
        starts_at=slot.starts_at,  # type: ignore[attr-defined]
    )


def _unwrap(adapter: object) -> InMemoryAdapter:
    """Fault wrappers proxy the adapter. Scoring needs the real one underneath."""
    inner = getattr(adapter, "inner", None)
    return inner if isinstance(inner, InMemoryAdapter) else adapter  # type: ignore[return-value]
