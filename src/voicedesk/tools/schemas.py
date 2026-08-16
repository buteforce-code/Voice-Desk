"""Strict input/output schemas for every tool.

G4: one function per action, strict schema in and out.  Nothing is passed as a
free-form dict.  `extra="forbid"` everywhere, so a model hallucinating an extra
argument fails validation instead of having it silently dropped.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    """Base for every tool payload. Unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Tier(str, Enum):
    """Risk tier from PROJECT.md 2. Set per tool, enforced in the registry."""

    AUTONOMOUS = "autonomous"
    HUMAN_REVIEW = "human_review"
    EXPLICIT_APPROVAL = "explicit_approval"
    PROHIBITED = "prohibited"


# Indian mobile: exactly 10 digits, first digit 6-9, optional +91/91 prefix.
#
# The previous pattern (^\+?[1-9]\d{7,14}$) accepted 8-15 digits. That let a
# 9-digit number through -- i.e. a real number with one digit dropped by ASR,
# which is the single most likely transcription failure on a noisy line. It
# would have validated cleanly and been written to the patient record, and the
# confirmation SMS would have gone to nobody. Found by eval case badinput-008.
#
# v1 is India-only (see PROJECT.md). Widen this deliberately, with a country
# field, if that ever changes -- do not relax it back to a permissive range.
Msisdn = Annotated[str, Field(pattern=r"^(?:\+?91)?[6-9]\d{9}$")]
IdempotencyKey = Annotated[str, Field(min_length=16, max_length=128)]


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class ToolContext(Strict):
    """Server-side truth about who is calling and in what state.

    Never populated from model output.  The registry builds this from the call
    session; a model cannot assert its own clinic_id or approval token.
    """

    clinic_id: UUID
    call_id: UUID
    trace_id: str
    state: str
    dry_run: bool = True
    approval_token: str | None = None
    speculative: bool = False


class ToolResult(Strict):
    """Uniform envelope. One shape for every tool, success or failure."""

    ok: bool
    data: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotent_replay: bool = False


# --------------------------------------------------------------------------
# get_clinic_info  -- AUTONOMOUS, side-effect free
# --------------------------------------------------------------------------


class GetClinicInfoIn(Strict):
    field: Literal[
        "opd_hours", "address", "consult_fee", "specialties",
        "doctors", "prep_instructions", "languages",
    ]
    specialty: str | None = None


class GetClinicInfoOut(Strict):
    field: str
    value: str
    source_key: str
    """Config key this came from. A claim without a source is a grounding bug."""


# --------------------------------------------------------------------------
# find_slots  -- AUTONOMOUS, side-effect free, speculatively prefetchable
# --------------------------------------------------------------------------


class FindSlotsIn(Strict):
    specialty: str | None = None
    doctor_id: UUID | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    limit: Annotated[int, Field(ge=1, le=10)] = 5


class SlotOut(Strict):
    slot_id: UUID
    doctor_id: UUID
    doctor_name: str
    specialty: str
    starts_at: datetime
    ends_at: datetime


class FindSlotsOut(Strict):
    slots: list[SlotOut]
    truncated: bool = False


# --------------------------------------------------------------------------
# find_appointments  -- AUTONOMOUS, side-effect free, but identity-gated.
#
# Added after eval authors independently inferred that this tool must exist:
# RescheduleIn and CancelIn both require an appointment_id, and nothing in the
# registry produced one. The agent was being asked to change a booking it had
# no way to locate. Found by ambiguous-008 / ambiguous-009.
# --------------------------------------------------------------------------


class FindAppointmentsIn(Strict):
    patient_msisdn: Msisdn
    identity_verified: Literal[True]
    """Reading a patient's bookings is reading their health data. Literal[True]
    makes an unverified lookup inexpressible, exactly as on reschedule/cancel.
    Without this the tool becomes a way to enumerate whether an arbitrary
    number has appointments here."""
    include_past: bool = False


class AppointmentOut(Strict):
    appointment_id: UUID
    doctor_name: str
    specialty: str
    starts_at: datetime
    status: Literal["confirmed"]


class FindAppointmentsOut(Strict):
    appointments: list[AppointmentOut]


# --------------------------------------------------------------------------
# hold_slot  -- AUTONOMOUS but MUTATING. Never speculative.
# --------------------------------------------------------------------------


class HoldSlotIn(Strict):
    slot_id: UUID
    ttl_seconds: Annotated[int, Field(ge=30, le=300)] = 120


class HoldSlotOut(Strict):
    slot_id: UUID
    held_until: datetime


# --------------------------------------------------------------------------
# confirm_booking  -- EXPLICIT_APPROVAL
# --------------------------------------------------------------------------


class ConfirmBookingIn(Strict):
    slot_id: UUID
    patient_msisdn: Msisdn
    patient_display_name: Annotated[str, Field(min_length=1, max_length=120)]
    idempotency_key: IdempotencyKey

    @field_validator("patient_display_name")
    @classmethod
    def _no_clinical_content(cls, v: str) -> str:
        """A name field is a name field. Callers describe symptoms into it;
        models helpfully pass them through. The schema refuses."""
        if len(v.split()) > 6:
            raise ValueError("display_name too long to be a name")
        return v


class ConfirmBookingOut(Strict):
    appointment_id: UUID
    slot_id: UUID
    starts_at: datetime
    doctor_name: str
    undo_deadline: datetime


# --------------------------------------------------------------------------
# reschedule_appointment / cancel_appointment  -- EXPLICIT_APPROVAL
# --------------------------------------------------------------------------


class RescheduleIn(Strict):
    appointment_id: UUID
    new_slot_id: UUID
    identity_verified: Literal[True]
    """Literal[True] makes 'I forgot to verify' a schema error, not a code path."""
    idempotency_key: IdempotencyKey


class RescheduleOut(Strict):
    appointment_id: UUID
    supersedes: UUID
    starts_at: datetime
    undo_deadline: datetime


class CancelIn(Strict):
    appointment_id: UUID
    identity_verified: Literal[True]
    reason: Annotated[str, Field(max_length=200)]
    idempotency_key: IdempotencyKey


class CancelOut(Strict):
    appointment_id: UUID
    cancelled_at: datetime
    undo_deadline: datetime


# --------------------------------------------------------------------------
# transfer_to_human  -- AUTONOMOUS. Always permitted, never blocked.
# --------------------------------------------------------------------------


class TransferIn(Strict):
    reason: Literal[
        "caller_requested", "low_confidence", "validator_failed",
        "clinical_request", "identity_failed", "out_of_scope", "error",
    ]
    context_summary: Annotated[str, Field(max_length=500)]


class TransferOut(Strict):
    transferred_to: str
    reason: str
