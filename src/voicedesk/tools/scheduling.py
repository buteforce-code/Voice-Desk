"""The eight tools. One function per action — no general-purpose escape hatch.

Prefer `confirm_booking(slot_id, msisdn, name)` over "access to the scheduler".
There is deliberately no `run_query`, no `call_hmis`, no `execute`.

Tiers come from the PROJECT.md risk register and are enforced in registry.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voicedesk.adapters.base import SchedulingAdapter
from voicedesk.tenants import Tenant
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.schemas import (
    CancelIn,
    CancelOut,
    ConfirmBookingIn,
    ConfirmBookingOut,
    FindAppointmentsIn,
    FindAppointmentsOut,
    FindSlotsIn,
    FindSlotsOut,
    GetClinicInfoIn,
    GetClinicInfoOut,
    HoldSlotIn,
    HoldSlotOut,
    RescheduleIn,
    RescheduleOut,
    Tier,
    ToolContext,
    TransferIn,
    TransferOut,
)

UNDO_WINDOW = timedelta(seconds=900)


class TenantConfig:
    """Loaded into memory at call start. Never fetched mid-turn — a network
    lookup on the hot path is a latency bug (see docs/LATENCY.md)."""

    def __init__(
        self,
        data: dict[str, str],
        escalation_msisdn: str,
        timezone: str = "Asia/Kolkata",
    ) -> None:
        self._data = data
        self.escalation_msisdn = escalation_msisdn
        self.timezone = timezone
        """The clinic's zone, needed to read a naive datetime the model sent.

        The prompt tells the model every time is clinic-local, so when it omits
        an offset that is what it means. Interpreting it as UTC instead shifts
        the search window by five and a half hours."""

    def get(self, field: str, specialty: str | None = None) -> tuple[str, str]:
        """Returns (value, source_key). The key is evidence: the dashboard
        renders it, and a claim with no source is a grounding bug."""
        key = f"{field}.{specialty.lower()}" if specialty else field
        if key in self._data:
            return self._data[key], key
        if field in self._data:
            return self._data[field], field
        raise KeyError(field)

    @classmethod
    def from_tenant(cls, tenant: Tenant) -> TenantConfig:
        """Build from a validated tenant file.

        The only supported way to construct one outside tests. Hard rule 8:
        tenant identity lives in config, never in code -- so the path from a
        YAML file to what the agent says about the clinic runs entirely through
        the loader's validation.
        """
        return cls(
            dict(tenant.info),
            escalation_msisdn=tenant.escalation_msisdn,
            timezone=tenant.timezone,
        )


def _clinic_local(value: datetime | None, timezone: str) -> datetime | None:
    """Attach the clinic's zone to a datetime the model sent without one.

    The model is told, every turn, that all times are clinic-local. When it
    omits the offset that is what it means, so this reads it that way rather
    than assuming UTC -- which would move the search window by five and a half
    hours and return an afternoon when the caller asked for a morning.

    Found by the eval suite's first full run: `find_slots` raised
    `TypeError: can't compare offset-naive and offset-aware datetimes` on
    ordinary model output, the registry converted it to `tool_failed`, and the
    agent spent the rest of the call apologising for a scheduler that was fine.
    """
    if value is None or value.tzinfo is not None:
        return value
    try:
        return value.replace(tzinfo=ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - config validates this
        return value.replace(tzinfo=UTC)


def register_scheduling_tools(
    registry: ToolRegistry,
    adapter: SchedulingAdapter,
    config: TenantConfig,
) -> None:
    """Wire the six tools. Called once at process start."""

    # -- AUTONOMOUS, side-effect free ------------------------------------

    @registry.register(
        "get_clinic_info",
        description=(
            "Look up a fact about this clinic: opening hours, address, consultation fee, "
            "specialties offered, doctors, preparation instructions, or languages spoken. Use this "
            "for ANY factual claim about the clinic — never answer from memory."
        ),
        tier=Tier.AUTONOMOUS,
        input_model=GetClinicInfoIn,
        output_model=GetClinicInfoOut,
        side_effect_free=True,
    )
    async def get_clinic_info(args: GetClinicInfoIn, ctx: ToolContext) -> GetClinicInfoOut:
        # Every answer carries the config key it came from. The dashboard
        # renders it as evidence; a claim without a source is a grounding bug.
        value, source_key = config.get(args.field, args.specialty)
        return GetClinicInfoOut(field=args.field, value=value, source_key=source_key)

    @registry.register(
        "find_slots",
        description=(
            "Find available appointment times. Filter by specialty or by a specific doctor. Only "
            "times returned by this tool may be offered to the caller."
        ),
        tier=Tier.AUTONOMOUS,
        input_model=FindSlotsIn,
        output_model=FindSlotsOut,
        side_effect_free=True,
    )
    async def find_slots(args: FindSlotsIn, ctx: ToolContext) -> FindSlotsOut:
        # The only speculatively-prefetchable tool with real latency value:
        # started on high-confidence intent before the caller finishes.
        #
        # The window is normalised here because this is the boundary that knows
        # the tenant. Models routinely emit "2026-08-23T00:00:00" with no
        # offset; pydantic accepts it as a naive datetime, the adapter compares
        # it against tz-aware slot times, and Python raises TypeError. The
        # registry catches that at the handler boundary and hands the agent
        # `tool_failed` -- so the most-used tool in the product fails on
        # ordinary model output, and the transcript shows an agent apologising
        # for a scheduler that is fine.
        slots = await adapter.find_slots(
            ctx.clinic_id,
            specialty=args.specialty,
            doctor_id=args.doctor_id,
            earliest=_clinic_local(args.earliest, config.timezone) or datetime.now(UTC),
            latest=_clinic_local(args.latest, config.timezone),
            limit=args.limit,
        )
        return FindSlotsOut(slots=slots, truncated=len(slots) >= args.limit)

    @registry.register(
        "find_appointments",
        description=(
            "List the CALLER'S OWN existing confirmed appointments. Requires a verified caller; it "
            "takes no phone number, because it can only ever return the appointments of the person "
            "on the line."
        ),
        tier=Tier.AUTONOMOUS,
        input_model=FindAppointmentsIn,
        output_model=FindAppointmentsOut,
        side_effect_free=True,
        requires_identity=True,
    )
    async def find_appointments(
        args: FindAppointmentsIn, ctx: ToolContext
    ) -> FindAppointmentsOut:
        # Side-effect free, so technically speculatable -- but the registry
        # refuses it until the identify state has completed a challenge, and
        # there is nothing to speculate on before then.
        #
        # The subject is ctx.verified_msisdn, never an argument. When the model
        # supplied the number this tool answered "does <any number> have
        # appointments here" for anyone who asked.
        assert ctx.verified_msisdn is not None  # noqa: S101 - guaranteed by requires_identity
        appts = await adapter.find_appointments(
            ctx.clinic_id,
            patient_msisdn=ctx.verified_msisdn,
            include_past=args.include_past,
        )
        return FindAppointmentsOut(appointments=appts)

    # -- AUTONOMOUS but MUTATING. Excluded from speculation. --------------

    @registry.register(
        "hold_slot",
        description=(
            "Reserve a slot for a couple of minutes while the caller decides. This is not a "
            "booking and must never be described to the caller as one."
        ),
        tier=Tier.AUTONOMOUS,
        input_model=HoldSlotIn,
        output_model=HoldSlotOut,
        side_effect_free=False,
    )
    async def hold_slot(args: HoldSlotIn, ctx: ToolContext) -> HoldSlotOut:
        # A hold is not a booking. It self-expires, is released on
        # abandoned/failed, and is never described to the caller as confirmed.
        held_until = await adapter.hold_slot(
            ctx.clinic_id, args.slot_id, ctx.call_id, args.ttl_seconds
        )
        return HoldSlotOut(slot_id=args.slot_id, held_until=held_until)

    # -- EXPLICIT_APPROVAL. Reachable only from state 'approval'. ---------

    @registry.register(
        "confirm_booking",
        description=(
            "Create a new appointment in the clinic's register. Only call this after the caller "
            "has explicitly agreed to a specific time you offered."
        ),
        tier=Tier.EXPLICIT_APPROVAL,
        input_model=ConfirmBookingIn,
        output_model=ConfirmBookingOut,
        side_effect_free=False,
    )
    async def confirm_booking(
        args: ConfirmBookingIn, ctx: ToolContext
    ) -> ConfirmBookingOut:
        # SlotUnavailable propagates: the agent re-offers rather than
        # inventing a booking that does not exist. registry.py turns it into
        # a failed ToolResult and an audit row.
        appt_id, starts_at, doctor_name = await adapter.confirm_booking(
            ctx.clinic_id,
            slot_id=args.slot_id,
            patient_msisdn=args.patient_msisdn,
            patient_display_name=args.patient_display_name,
            call_id=ctx.call_id,
        )
        return ConfirmBookingOut(
            appointment_id=appt_id,
            slot_id=args.slot_id,
            starts_at=starts_at,
            doctor_name=doctor_name,
            undo_deadline=datetime.now(UTC) + UNDO_WINDOW,
        )

    @registry.register(
        "reschedule_appointment",
        description=(
            "Move one of the caller's existing appointments to a different slot. Requires a "
            "verified caller."
        ),
        tier=Tier.EXPLICIT_APPROVAL,
        input_model=RescheduleIn,
        output_model=RescheduleOut,
        side_effect_free=False,
        requires_identity=True,
    )
    async def reschedule_appointment(
        args: RescheduleIn, ctx: ToolContext
    ) -> RescheduleOut:
        # Two independent gates, neither of them a model assertion: the registry
        # refuses without ctx.identity_verified, and the adapter scopes the
        # lookup to the verified number -- so someone else's appointment_id is
        # not found rather than found and then refused.
        assert ctx.verified_msisdn is not None  # noqa: S101 - guaranteed by requires_identity
        new_id, starts_at = await adapter.reschedule(
            ctx.clinic_id,
            appointment_id=args.appointment_id,
            new_slot_id=args.new_slot_id,
            patient_msisdn=ctx.verified_msisdn,
            call_id=ctx.call_id,
        )
        return RescheduleOut(
            appointment_id=new_id,
            supersedes=args.appointment_id,
            starts_at=starts_at,
            undo_deadline=datetime.now(UTC) + UNDO_WINDOW,
        )

    @registry.register(
        "cancel_appointment",
        description=(
            "Cancel one of the caller's existing appointments. Requires a verified caller."
        ),
        tier=Tier.EXPLICIT_APPROVAL,
        input_model=CancelIn,
        output_model=CancelOut,
        side_effect_free=False,
        requires_identity=True,
    )
    async def cancel_appointment(args: CancelIn, ctx: ToolContext) -> CancelOut:
        assert ctx.verified_msisdn is not None  # noqa: S101 - guaranteed by requires_identity
        cancelled_at = await adapter.cancel(
            ctx.clinic_id,
            appointment_id=args.appointment_id,
            reason=args.reason,
            patient_msisdn=ctx.verified_msisdn,
            call_id=ctx.call_id,
        )
        return CancelOut(
            appointment_id=args.appointment_id,
            cancelled_at=cancelled_at,
            undo_deadline=datetime.now(UTC) + UNDO_WINDOW,
        )

    # -- AUTONOMOUS. The safe default, never blocked. ---------------------

    @registry.register(
        "transfer_to_human",
        description=(
            "Hand the call to a person at the front desk. Always available. Use it whenever the "
            "caller asks for a human, whenever anything clinical comes up, and whenever you are "
            "unsure — transferring is never the wrong answer."
        ),
        tier=Tier.AUTONOMOUS,
        input_model=TransferIn,
        output_model=TransferOut,
        side_effect_free=True,
    )
    async def transfer_to_human(args: TransferIn, ctx: ToolContext) -> TransferOut:
        # Always permitted from any state. If the agent is unsure, transferring
        # is correct behaviour, not failure — and a caller who asks for a human
        # gets one immediately, with no retention attempt.
        return TransferOut(
            transferred_to=config.escalation_msisdn, reason=args.reason
        )


# There is no tool for: placing an outbound call (C12), clinical advice (C13),
# retrieving results or prescriptions (C14), taking payment (C15), deleting
# anything (C16), or changing tenant config (C17).
#
# Not "there is a tool that refuses" — there is no tool. That is what the
# prohibited tier means, and tests/test_prohibited.py asserts it.
_PROHIBITED_BY_ABSENCE: frozenset[str] = frozenset({
    "outbound_call", "give_medical_advice", "get_test_results",
    "get_prescription", "take_payment", "delete_appointment",
    "update_clinic_config",
})
