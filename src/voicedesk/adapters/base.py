"""Scheduling adapter interface.

The seam between Voice Desk and whatever the clinic actually runs.

Two implementations are planned:

  * `PostgresAdapter` -- the designated source of truth for clinics with no
    scheduling API, reconciled to the clinic's own register daily. This is the
    one that exists.

  * `HmisAdapter` -- a real integration (Halemind / KareXpert / Practo Ray /
    SoftClinic). **Deliberately unimplemented.** No clinic is engaged, so
    writing an adapter against a guessed API shape would be fiction. When a
    real clinic exists, it implements this Protocol and nothing above it
    changes.

Everything above this interface -- tools, state machine, validators, evals --
is written against the Protocol, never against Postgres.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from voicedesk.tools.schemas import SlotOut


class SchedulingError(Exception):
    """Adapter-level failure. Callers translate to a ToolResult; the message
    never reaches the patient."""


class SlotUnavailable(SchedulingError):
    """Lost the race, or the hold expired. Recoverable — re-offer."""


class AppointmentNotFound(SchedulingError):
    pass


class SchedulingAdapter(Protocol):
    """Every method is tenant-scoped. No method may cross clinic_id."""

    async def find_slots(
        self,
        clinic_id: UUID,
        *,
        specialty: str | None,
        doctor_id: UUID | None,
        earliest: datetime | None,
        latest: datetime | None,
        limit: int,
    ) -> list[SlotOut]: ...

    async def hold_slot(
        self, clinic_id: UUID, slot_id: UUID, call_id: UUID, ttl_seconds: int
    ) -> datetime:
        """Short TTL reservation. Self-expires. Not a booking, and never
        visible to the patient as one."""
        ...

    async def release_hold(self, clinic_id: UUID, call_id: UUID) -> None:
        """Called on abandoned/failed. A dropped call must not park a slot."""
        ...

    async def confirm_booking(
        self,
        clinic_id: UUID,
        *,
        slot_id: UUID,
        patient_msisdn: str,
        patient_display_name: str,
        call_id: UUID,
    ) -> tuple[UUID, datetime, str]:
        """Returns (appointment_id, starts_at, doctor_name).

        Must be safe against concurrent confirmation of the same slot — the
        partial unique index in 0001_init.sql makes the loser a constraint
        violation rather than a silent double-book.
        """
        ...

    async def reschedule(
        self, clinic_id: UUID, *, appointment_id: UUID, new_slot_id: UUID, call_id: UUID
    ) -> tuple[UUID, datetime]:
        """Soft-versioned: writes a new row, sets superseded_by on the old.
        Returns (new_appointment_id, starts_at). Never overwrites."""
        ...

    async def cancel(
        self, clinic_id: UUID, *, appointment_id: UUID, reason: str, call_id: UUID
    ) -> datetime:
        """Soft-cancel. Returns cancelled_at. Never deletes."""
        ...

    async def undo(self, clinic_id: UUID, *, appointment_id: UUID) -> None:
        """Restore the prior version. Because appointments are soft-versioned,
        this is a pointer move, not a reconstruction.

        G3 requires undo to exist for every reversible executed action
        *before that action ships*.
        """
        ...
