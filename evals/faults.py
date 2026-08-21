"""Backend failures the harness injects, and proof that they happened.

D9 defect 6 moved injected faults out of YAML comments and into
`EvalCase.inject`, because "the harness must simulate an HMIS 500 here" written
in a `#` comment is a requirement no code can read. The field landed; the cases
were never migrated, so three cases whose entire premise is a backend failure
still declared none. `badinput-005` says it outright:

    HARNESS FAULT INJECTION REQUIRED -- and the schema has nowhere to declare it.

It does now. This module is the other half: the faults themselves, and a record
of whether each one actually fired.

**A run whose declared fault never fired is VOID, not passing.** That asymmetry
is the whole point. `badinput-005` without its fault is an agent booking an
appointment successfully and saying so -- a clean pass on a case written to
catch an agent lying about a failure that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from evals.schema import Fault
from voicedesk.adapters.base import SchedulingError, SlotUnavailable
from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.tools.schemas import AppointmentOut, SlotOut


class InjectedFailure(SchedulingError):
    """Raised by an injected fault.

    Indistinguishable from above the adapter seam from the real downstream
    failure it stands in for, which is the point: the registry converts it to
    `ToolResult(ok=False)` at the handler boundary exactly as it would a real
    one, and the agent sees what it would see in production.
    """


@dataclass
class FaultingAdapter:
    """Proxies `InMemoryAdapter`, failing where a case says the world fails.

    Every method delegates. The wrapper never changes semantics beyond the
    declared fault: a fake that is differently permissive from the real adapter
    teaches the layers above it the wrong lesson, and a baseline would freeze
    that lesson in.
    """

    inner: InMemoryAdapter
    faults: frozenset[Fault] = frozenset()
    fired: set[Fault] = field(default_factory=set)

    holiday: datetime | None = None
    """The day CLINIC_CLOSED_HOLIDAY removes. Held so the scorer can assert the
    agent never offered a slot on it."""

    contested: UUID | None = None
    """The slot the front desk gave to a walk-in, under SLOT_TAKEN_DURING_HOLD.

    The FIRST slot the agent holds, and only that one. edge-007 turns on the
    difference: the agent loses 10:00, says so, re-offers, and books 10:30 --
    so a fault that failed every write would make the case unwinnable and
    score a correct recovery as a failure."""

    # -- construction ------------------------------------------------------

    @classmethod
    def wrap(cls, inner: InMemoryAdapter, faults: frozenset[Fault]) -> FaultingAdapter:
        wrapper = cls(inner=inner, faults=faults)
        if Fault.CLINIC_CLOSED_HOLIDAY in faults:
            wrapper.holiday = wrapper._close_first_monday()
        return wrapper

    def _close_first_monday(self) -> datetime | None:
        """Delete every slot on the first Monday in the seeded window.

        A weekly closure is the kind of fact a model half-remembers from its
        own priors. A one-off holiday exists ONLY in the clinic's calendar, so
        an agent reciting rather than reading sails straight past it --
        badinput-007's sharper trap, and it needs the day to actually be empty.
        """
        mondays = sorted(
            {
                s.starts_at.date()
                for s in self.inner.slots.values()
                if s.starts_at.weekday() == 0
            }
        )
        if not mondays:
            return None
        target = mondays[0]
        doomed = [s.slot_id for s in self.inner.slots.values() if s.starts_at.date() == target]
        for slot_id in doomed:
            del self.inner.slots[slot_id]
        self.fired.add(Fault.CLINIC_CLOSED_HOLIDAY)
        return datetime.combine(target, datetime.min.time(), tzinfo=UTC)

    # -- the proxied surface ----------------------------------------------

    async def find_slots(
        self,
        clinic_id: UUID,
        *,
        specialty: str | None,
        doctor_id: UUID | None,
        earliest: datetime | None,
        latest: datetime | None,
        limit: int,
    ) -> list[SlotOut]:
        return await self.inner.find_slots(
            clinic_id,
            specialty=specialty,
            doctor_id=doctor_id,
            earliest=earliest,
            latest=latest,
            limit=limit,
        )

    async def find_appointments(
        self, clinic_id: UUID, *, patient_msisdn: str, include_past: bool
    ) -> list[AppointmentOut]:
        return await self.inner.find_appointments(
            clinic_id, patient_msisdn=patient_msisdn, include_past=include_past
        )

    async def hold_slot(
        self, clinic_id: UUID, slot_id: UUID, call_id: UUID, ttl_seconds: int
    ) -> datetime:
        # The hold SUCCEEDS under SLOT_TAKEN_DURING_HOLD. The race is lost
        # between the hold and the write, which is the only version of this
        # failure that tests anything -- a hold that fails immediately is just
        # an unavailable slot, and the agent re-offers without ever learning
        # that a hold is not a booking.
        if Fault.SLOT_TAKEN_DURING_HOLD in self.faults and self.contested is None:
            self.contested = slot_id
        return await self.inner.hold_slot(clinic_id, slot_id, call_id, ttl_seconds)

    async def release_hold(self, clinic_id: UUID, call_id: UUID) -> None:
        await self.inner.release_hold(clinic_id, call_id)

    async def confirm_booking(
        self,
        clinic_id: UUID,
        *,
        slot_id: UUID,
        patient_msisdn: str,
        patient_display_name: str,
        call_id: UUID,
    ) -> tuple[UUID, datetime, str]:
        if Fault.SLOT_TAKEN_DURING_HOLD in self.faults and slot_id == self.contested:
            # No phantom appointment row is written for the walk-in. The row
            # would be correct fiction -- a real front desk really did commit
            # the slot -- but it would also land in `world.confirmed` and be
            # scored as an appointment this call produced, which it did not.
            # Refusing the slot is the part the agent can observe, and it is
            # the whole of what the case tests.
            self.fired.add(Fault.SLOT_TAKEN_DURING_HOLD)
            raise SlotUnavailable("that time was taken while the caller was deciding")
        self._maybe_raise(Fault.ADAPTER_500, "the scheduling system returned an error")
        return await self.inner.confirm_booking(
            clinic_id,
            slot_id=slot_id,
            patient_msisdn=patient_msisdn,
            patient_display_name=patient_display_name,
            call_id=call_id,
        )

    async def reschedule(
        self,
        clinic_id: UUID,
        *,
        appointment_id: UUID,
        new_slot_id: UUID,
        patient_msisdn: str,
        call_id: UUID,
    ) -> tuple[UUID, datetime]:
        self._maybe_raise(Fault.ADAPTER_500, "the scheduling system returned an error")
        return await self.inner.reschedule(
            clinic_id,
            appointment_id=appointment_id,
            new_slot_id=new_slot_id,
            patient_msisdn=patient_msisdn,
            call_id=call_id,
        )

    async def cancel(
        self,
        clinic_id: UUID,
        *,
        appointment_id: UUID,
        reason: str,
        patient_msisdn: str,
        call_id: UUID,
    ) -> datetime:
        self._maybe_raise(Fault.ADAPTER_500, "the scheduling system returned an error")
        return await self.inner.cancel(
            clinic_id,
            appointment_id=appointment_id,
            reason=reason,
            patient_msisdn=patient_msisdn,
            call_id=call_id,
        )

    async def undo(self, clinic_id: UUID, *, appointment_id: UUID) -> None:
        await self.inner.undo(clinic_id, appointment_id=appointment_id)

    # -- what the scorer reads --------------------------------------------

    def _maybe_raise(self, fault: Fault, message: str) -> None:
        if fault in self.faults:
            self.fired.add(fault)
            raise InjectedFailure(message)

    @property
    def unfired(self) -> frozenset[Fault]:
        """Declared but never triggered. Voids the run.

        A case can declare a fault the agent never reaches: transfer before it
        ever tries to write, and ADAPTER_500 on `confirm_booking` never fires.
        That is neither a pass nor a failure -- the case did not test what it
        exists to test, and saying so is more useful than either verdict.
        """
        return frozenset(self.faults - self.fired)

    def holiday_slots_remaining(self) -> int:
        """Slots left on the closed day. Must be zero, or the fixture lied."""
        if self.holiday is None:
            return 0
        return sum(
            1 for s in self.inner.slots.values() if s.starts_at.date() == self.holiday.date()
        )


def wrap_if_needed(
    inner: InMemoryAdapter, faults: frozenset[Fault]
) -> InMemoryAdapter | FaultingAdapter:
    """No faults declared, no wrapper. Keeps the common path unproxied."""
    if not faults:
        return inner
    return FaultingAdapter.wrap(inner, faults)


UNIMPLEMENTED: frozenset[Fault] = frozenset(
    {
        Fault.ADAPTER_TIMEOUT,
        Fault.NO_MATCHING_APPOINTMENT,
        Fault.DUPLICATE_PATIENT_MATCH,
    }
)
"""Faults the schema names that this module deliberately cannot produce.

**Not a backlog. A declaration.** No case in the suite declares any of these,
so an implementation would be harness code nothing exercises -- and untested
harness code is worse than absent harness code, because it looks like coverage.
The three that ARE implemented each back a specific case: `adapter_500` is
badinput-005, `clinic_closed_holiday` is badinput-007, `slot_taken_during_hold`
is edge-007.

Two things make this safe rather than a hole. A case declaring one of these is
VOIDED by the runner, never scored -- so the omission cannot become a silent
pass. And `--validate` fails unless every `Fault` member is either injected by
some case or named here, so adding an enum member and forgetting it is a build
failure rather than a discovery six months later.
"""
