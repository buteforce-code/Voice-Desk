"""The six tools. One function per action — no general-purpose escape hatch.

Prefer `confirm_booking(slot_id, msisdn, name)` over "access to the scheduler".
There is deliberately no `run_query`, no `call_hmis`, no `execute`.

Tiers come from the PROJECT.md risk register and are enforced in registry.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from voicedesk.adapters.base import SchedulingAdapter
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

    def __init__(self, data: dict[str, str], escalation_msisdn: str) -> None:
        self._data = data
        self.escalation_msisdn = escalation_msisdn

    def get(self, field: str, specialty: str | None = None) -> tuple[str, str]:
        key = f"{field}.{specialty}" if specialty else field
        if key in self._data:
            return self._data[key], key
        if field in self._data:
            return self._data[field], field
        raise KeyError(field)


def register_scheduling_tools(
    registry: ToolRegistry,
    adapter: SchedulingAdapter,
    config: TenantConfig,
) -> None:
    """Wire the six tools. Called once at process start."""

    # -- AUTONOMOUS, side-effect free ------------------------------------

    @registry.register(
        "get_clinic_info",
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
        tier=Tier.AUTONOMOUS,
        input_model=FindSlotsIn,
        output_model=FindSlotsOut,
        side_effect_free=True,
    )
    async def find_slots(args: FindSlotsIn, ctx: ToolContext) -> FindSlotsOut:
        # The only speculatively-prefetchable tool with real latency value:
        # started on high-confidence intent before the caller finishes.
        slots = await adapter.find_slots(
            ctx.clinic_id,
            specialty=args.specialty,
            doctor_id=args.doctor_id,
            earliest=args.earliest or datetime.now(UTC),
            latest=args.latest,
            limit=args.limit,
        )
        return FindSlotsOut(slots=slots, truncated=len(slots) >= args.limit)

    @registry.register(
        "find_appointments",
        tier=Tier.AUTONOMOUS,
        input_model=FindAppointmentsIn,
        output_model=FindAppointmentsOut,
        side_effect_free=True,
    )
    async def find_appointments(
        args: FindAppointmentsIn, ctx: ToolContext
    ) -> FindAppointmentsOut:
        # Side-effect free, so technically speculatable -- but identity_verified
        # is Literal[True], and identity is not established until the identify
        # state completes. There is nothing to speculate on before then.
        appts = await adapter.find_appointments(
            ctx.clinic_id,
            patient_msisdn=args.patient_msisdn,
            include_past=args.include_past,
        )
        return FindAppointmentsOut(appointments=appts)

    # -- AUTONOMOUS but MUTATING. Excluded from speculation. --------------

    @registry.register(
        "hold_slot",
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
        tier=Tier.EXPLICIT_APPROVAL,
        input_model=RescheduleIn,
        output_model=RescheduleOut,
        side_effect_free=False,
    )
    async def reschedule_appointment(
        args: RescheduleIn, ctx: ToolContext
    ) -> RescheduleOut:
        # identity_verified is Literal[True] in the schema, so an unverified
        # reschedule fails validation before reaching this line.
        new_id, starts_at = await adapter.reschedule(
            ctx.clinic_id,
            appointment_id=args.appointment_id,
            new_slot_id=args.new_slot_id,
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
        tier=Tier.EXPLICIT_APPROVAL,
        input_model=CancelIn,
        output_model=CancelOut,
        side_effect_free=False,
    )
    async def cancel_appointment(args: CancelIn, ctx: ToolContext) -> CancelOut:
        cancelled_at = await adapter.cancel(
            ctx.clinic_id,
            appointment_id=args.appointment_id,
            reason=args.reason,
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
