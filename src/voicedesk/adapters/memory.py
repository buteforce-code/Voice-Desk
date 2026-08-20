"""In-memory scheduling adapter — for local runs, evals and tests.

**Not for production.** No persistence, no RLS, no audit durability. It exists
so the system can be exercised end to end without a database, which matters for
three reasons:

  * The G5 eval harness has to drive 58 cases. Standing up Postgres for each is
    friction that gets the suite run less often.
  * Someone should be able to clone the repo and watch a booking happen without
    provisioning anything.
  * `PostgresAdapter` cannot be exercised at all until a DSN exists, so every
    behaviour above the adapter seam would otherwise be untested against a real
    implementation.

It deliberately mirrors `PostgresAdapter`'s *semantics*, not just its signature:
one live appointment per slot, soft-versioned reschedule, soft cancel, undo as a
pointer move, and lookups scoped by both `clinic_id` and the verified msisdn. A
fake that is more permissive than the real thing teaches the layers above it the
wrong lesson, and the eval baseline would encode that lesson.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voicedesk.adapters.base import (
    AppointmentNotFound,
    SlotUnavailable,
)
from voicedesk.tenants import Tenant
from voicedesk.tools.schemas import AppointmentOut, SlotOut, normalize_msisdn

SLOT_MINUTES = 20


@dataclass
class _Slot:
    slot_id: UUID
    clinic_id: UUID
    doctor_id: UUID
    doctor_name: str
    specialty: str
    starts_at: datetime
    ends_at: datetime
    held_until: datetime | None = None
    held_by_call: UUID | None = None


@dataclass
class _Appointment:
    appointment_id: UUID
    clinic_id: UUID
    patient_msisdn: str
    doctor_name: str
    specialty: str
    slot_id: UUID
    starts_at: datetime
    status: str = "confirmed"
    version: int = 1
    supersedes: UUID | None = None
    superseded_by: UUID | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


@dataclass
class InMemoryAdapter:
    """Implements `SchedulingAdapter` over dicts."""

    tenant: Tenant
    slots: dict[UUID, _Slot] = field(default_factory=dict)
    appointments: dict[UUID, _Appointment] = field(default_factory=dict)

    @classmethod
    def seeded(
        cls,
        tenant: Tenant,
        *,
        start: datetime | None = None,
        days: int = 14,
    ) -> InMemoryAdapter:
        """Generate a fortnight of slots from the tenant's OPD hours.

        Sundays are skipped because the demo tenant's config says the clinic is
        closed then. Reading the closure out of config rather than hardcoding it
        keeps the fixture honest: if someone edits the hours, the slots move.
        """
        adapter = cls(tenant=tenant)
        begin = (start or datetime.now(UTC)).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(days=1)

        windows = _parse_opd_windows(tenant.info.get("opd_hours", ""))
        for day_offset in range(days):
            day = begin + timedelta(days=day_offset)
            # Sunday in the CLINIC'S calendar, not the server's.
            if day.astimezone(_clinic_zone(tenant.timezone)).weekday() == 6:
                continue
            for doctor in tenant.doctors:
                if not doctor.active:
                    continue
                for window_start, window_end in windows:
                    adapter._make_day_slots(doctor, day, window_start, window_end)
        return adapter

    def _make_day_slots(
        self, doctor: object, day: datetime, start: time, end: time
    ) -> None:
        """Build the day's slots in the CLINIC'S timezone, then store as UTC.

        This used to apply the config's local hour numbers directly to a UTC
        datetime. "9:00 AM to 1:00 PM" became 09:00Z, which is 14:30 in
        Asia/Kolkata -- an hour the clinic is shut. A live run caught it: the
        agent offered a 9am slot and correctly read it back to the caller as
        "2:30 PM IST", which was the first visible sign that the two disagreed.

        Every eval case about business hours would have been scored against
        slots that could not exist.
        """
        zone = _clinic_zone(self.tenant.timezone)
        local_day = day.astimezone(zone)
        cursor = local_day.replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0
        )
        stop = local_day.replace(
            hour=end.hour, minute=end.minute, second=0, microsecond=0
        )
        while cursor + timedelta(minutes=SLOT_MINUTES) <= stop:
            slot_id = uuid4()
            self.slots[slot_id] = _Slot(
                slot_id=slot_id,
                clinic_id=self.tenant.clinic_id,
                doctor_id=doctor.doctor_id,  # type: ignore[attr-defined]
                doctor_name=doctor.full_name,  # type: ignore[attr-defined]
                specialty=doctor.specialty,  # type: ignore[attr-defined]
                starts_at=cursor,
                ends_at=cursor + timedelta(minutes=SLOT_MINUTES),
            )
            cursor += timedelta(minutes=SLOT_MINUTES)

    # -- reads -----------------------------------------------------------

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
        now = datetime.now(UTC)
        floor = earliest or now
        booked = {
            a.slot_id for a in self.appointments.values() if a.status == "confirmed"
        }

        found = [
            s
            for s in self.slots.values()
            if s.clinic_id == clinic_id
            and s.starts_at >= floor
            and (latest is None or s.starts_at <= latest)
            and (specialty is None or s.specialty.lower() == specialty.lower())
            and (doctor_id is None or s.doctor_id == doctor_id)
            and (s.held_until is None or s.held_until < now)
            and s.slot_id not in booked
        ]
        found.sort(key=lambda s: (s.starts_at, str(s.slot_id)))

        return [
            SlotOut(
                slot_id=s.slot_id,
                doctor_id=s.doctor_id,
                doctor_name=s.doctor_name,
                specialty=s.specialty,
                starts_at=s.starts_at,
                ends_at=s.ends_at,
            )
            for s in itertools.islice(found, limit)
        ]

    async def find_appointments(
        self, clinic_id: UUID, *, patient_msisdn: str, include_past: bool
    ) -> list[AppointmentOut]:
        wanted = normalize_msisdn(patient_msisdn)
        now = datetime.now(UTC)

        found = [
            a
            for a in self.appointments.values()
            if a.clinic_id == clinic_id
            and normalize_msisdn(a.patient_msisdn) == wanted
            and a.status == "confirmed"
            and (include_past or a.starts_at >= now)
        ]
        found.sort(key=lambda a: a.starts_at)

        return [
            AppointmentOut(
                appointment_id=a.appointment_id,
                doctor_name=a.doctor_name,
                specialty=a.specialty,
                starts_at=a.starts_at,
                status="confirmed",
            )
            for a in found
        ]

    # -- holds -----------------------------------------------------------

    async def hold_slot(
        self, clinic_id: UUID, slot_id: UUID, call_id: UUID, ttl_seconds: int
    ) -> datetime:
        slot = self.slots.get(slot_id)
        now = datetime.now(UTC)
        booked = any(
            a.slot_id == slot_id and a.status == "confirmed"
            for a in self.appointments.values()
        )

        if slot is None or slot.clinic_id != clinic_id or booked:
            raise SlotUnavailable("slot is unavailable")
        if slot.held_until and slot.held_until > now and slot.held_by_call != call_id:
            raise SlotUnavailable("slot is held by another call")

        slot.held_until = now + timedelta(seconds=ttl_seconds)
        slot.held_by_call = call_id
        return slot.held_until

    async def release_hold(self, clinic_id: UUID, call_id: UUID) -> None:
        for slot in self.slots.values():
            if slot.clinic_id == clinic_id and slot.held_by_call == call_id:
                slot.held_until = None
                slot.held_by_call = None

    # -- writes ----------------------------------------------------------

    async def confirm_booking(
        self,
        clinic_id: UUID,
        *,
        slot_id: UUID,
        patient_msisdn: str,
        patient_display_name: str,
        call_id: UUID,
    ) -> tuple[UUID, datetime, str]:
        slot = self.slots.get(slot_id)
        if slot is None or slot.clinic_id != clinic_id:
            raise SlotUnavailable("no such slot")
        if slot.starts_at <= datetime.now(UTC):
            raise SlotUnavailable("slot has passed")

        # The partial unique index, by hand. Mirrors the real constraint so a
        # race resolves the same way here as in Postgres.
        if any(
            a.slot_id == slot_id and a.status == "confirmed"
            for a in self.appointments.values()
        ):
            raise SlotUnavailable("slot was booked by another caller")

        appointment_id = uuid4()
        self.appointments[appointment_id] = _Appointment(
            appointment_id=appointment_id,
            clinic_id=clinic_id,
            patient_msisdn=normalize_msisdn(patient_msisdn),
            doctor_name=slot.doctor_name,
            specialty=slot.specialty,
            slot_id=slot_id,
            starts_at=slot.starts_at,
        )
        return appointment_id, slot.starts_at, slot.doctor_name

    async def reschedule(
        self,
        clinic_id: UUID,
        *,
        appointment_id: UUID,
        new_slot_id: UUID,
        patient_msisdn: str,
        call_id: UUID,
    ) -> tuple[UUID, datetime]:
        old = self._owned(clinic_id, appointment_id, patient_msisdn)
        if old is None or old.status != "confirmed":
            raise AppointmentNotFound("no confirmed appointment with that id")

        slot = self.slots.get(new_slot_id)
        if slot is None or slot.clinic_id != clinic_id:
            raise SlotUnavailable("no such slot")
        if slot.starts_at <= datetime.now(UTC):
            raise SlotUnavailable("slot has passed")

        # Demote first, exactly as the SQL does -- otherwise rescheduling onto
        # the slot already held trips the one-live-per-slot rule against the
        # row being replaced.
        old.status = "superseded"

        if any(
            a.slot_id == new_slot_id and a.status == "confirmed"
            for a in self.appointments.values()
        ):
            old.status = "confirmed"  # restore before propagating
            raise SlotUnavailable("target slot was booked by another caller")

        new_id = uuid4()
        self.appointments[new_id] = replace(
            old,
            appointment_id=new_id,
            slot_id=new_slot_id,
            starts_at=slot.starts_at,
            doctor_name=slot.doctor_name,
            specialty=slot.specialty,
            status="confirmed",
            version=old.version + 1,
            supersedes=appointment_id,
            superseded_by=None,
        )
        old.superseded_by = new_id
        return new_id, slot.starts_at

    async def cancel(
        self,
        clinic_id: UUID,
        *,
        appointment_id: UUID,
        reason: str,
        patient_msisdn: str,
        call_id: UUID,
    ) -> datetime:
        appointment = self._owned(clinic_id, appointment_id, patient_msisdn)
        if appointment is None or appointment.status != "confirmed":
            raise AppointmentNotFound("no confirmed appointment with that id")

        appointment.status = "cancelled"
        appointment.cancel_reason = reason
        appointment.cancelled_at = datetime.now(UTC)
        return appointment.cancelled_at

    async def undo(self, clinic_id: UUID, *, appointment_id: UUID) -> None:
        appointment = self.appointments.get(appointment_id)
        if appointment is None or appointment.clinic_id != clinic_id:
            raise AppointmentNotFound("no appointment with that id")

        if appointment.supersedes is not None:
            appointment.status = "superseded"
            appointment.superseded_by = None
            predecessor = self.appointments[appointment.supersedes]
            predecessor.status = "confirmed"
            predecessor.superseded_by = None
            predecessor.cancelled_at = None
            predecessor.cancel_reason = None
            return

        if appointment.status == "cancelled":
            appointment.status = "confirmed"
            appointment.cancelled_at = None
            appointment.cancel_reason = None
            return

        appointment.status = "cancelled"
        appointment.cancelled_at = datetime.now(UTC)
        appointment.cancel_reason = "undone within the undo window"

    # -- helpers ----------------------------------------------------------

    def _owned(
        self, clinic_id: UUID, appointment_id: UUID, patient_msisdn: str
    ) -> _Appointment | None:
        """Scoped by tenant AND verified caller, like the SQL join.

        Someone else's appointment_id is not-found rather than forbidden, so a
        guessed id leaks nothing about whether it exists.
        """
        appointment = self.appointments.get(appointment_id)
        if appointment is None or appointment.clinic_id != clinic_id:
            return None
        if normalize_msisdn(appointment.patient_msisdn) != normalize_msisdn(
            patient_msisdn
        ):
            return None
        return appointment


_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.IGNORECASE)


def _parse_opd_windows(opd_hours: str) -> list[tuple[time, time]]:
    """Pull `9:00 AM to 1:00 PM` style ranges out of the config string.

    Falls back to a single morning window if nothing parses, so a reworded
    config produces a usable fixture instead of a clinic with no slots at all --
    which would look like "fully booked" to every caller in a local run.
    """
    found = [
        time(
            hour=(int(h) % 12) + (12 if ap.lower() == "pm" else 0),
            minute=int(m),
        )
        for h, m, ap in _TIME.findall(opd_hours)
    ]
    windows = [
        (found[i], found[i + 1])
        for i in range(0, len(found) - 1, 2)
        if found[i] < found[i + 1]
    ]
    return windows or [(time(9, 0), time(13, 0))]


def _clinic_zone(name: str) -> ZoneInfo:
    """Resolve the tenant's timezone, loudly.

    Windows ships no system tz database, so `ZoneInfo("Asia/Kolkata")` raises
    unless `tzdata` is installed -- which it was not, meaning nothing in the
    project could resolve the timezone its own tenant config declares. It is a
    declared dependency now; this message exists for the slim-container version
    of the same surprise.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment
        raise RuntimeError(
            f"cannot resolve timezone {name!r}. Install `tzdata` -- without a "
            f"tz database every appointment time is silently wrong."
        ) from exc
