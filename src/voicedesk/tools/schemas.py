"""Strict input/output schemas for every tool.

G4: one function per action, strict schema in and out.  Nothing is passed as a
free-form dict.  `extra="forbid"` everywhere, so a model hallucinating an extra
argument fails validation instead of having it silently dropped.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_BARE_CLOCK = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
"""`17:00` or `17:00:30`. Anchored with `fullmatch` at the call site, so a real
ISO timestamp -- which contains a colon-separated time but much else besides --
is never mistaken for one and is left for pydantic to parse properly."""


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


def normalize_msisdn(value: str) -> str:
    """Reduce an Indian mobile to its bare ten digits.

    The pattern above accepts `+919876543210`, `919876543210` and
    `9876543210` as the same subscriber, so any equality check between two
    msisdns has to normalise first or it will reject the right caller for
    saying their number a different way than the record stores it.
    """
    digits = value.removeprefix("+").removeprefix("91")
    return digits
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

    identity_verified: bool = False
    """Set by the state machine when the `identify` state completes a DOB
    challenge against the patient record -- never by the model.

    This used to be an `identity_verified: Literal[True]` field on the tool
    INPUT schemas, which the model filled in. Since `Literal[True]` admits
    exactly one value, the model always set it and validation always passed:
    a control that could not fail is not a control. `bad_input-009` says so in
    as many words -- "it does nothing whatsoever to stop a model from writing
    True because the field demanded it" -- while `malicious-003` asserts the
    opposite, that an unverified cancellation "is not expressible in the
    schema". The eval set contradicted itself, and the pessimistic case was
    the correct one.
    """

    verified_msisdn: str | None = None
    """The number that actually passed the challenge. Identity-gated tools read
    the subject from here rather than accepting one from the model, so
    "whose appointments?" is not a question the model gets to answer."""

    caller_msisdn: str | None = None
    """The ANI -- the number this call arrived on, from the telephony leg.

    Known for every call, and NOT proof of anything: a shared household handset
    is the norm here (see `ambiguous-003`), so this identifies a phone and never
    a person. `verified_msisdn` is the one that survived a challenge, and the
    two are kept apart so a tool cannot reach for whichever is populated.

    A new booking writes this. Reading or changing an EXISTING appointment
    reads `verified_msisdn` and nothing else."""


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
        "doctors", "prep_instructions", "languages", "default_specialty",
    ]
    """`default_specialty` is the clinic's own routing policy: where it starts a
    caller who does not yet know which department they need.

    It is deliberately a CONFIG LOOKUP and not a judgement. The value is the
    same sentence whatever the caller has described, which is the test that
    separates it from triage -- an agent that answered "neurology" to a
    headache and "gastroenterology" to stomach pain would be practising
    medicine. Answering "General Medicine, that is where we start everyone" is
    reading the clinic's front-desk policy aloud, and it is what a human
    receptionist says twenty times a day."""
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

    @field_validator("earliest", "latest", mode="before")
    @classmethod
    def _expand_bare_clock_time(cls, v: object) -> object:
        """Accept `"17:00"` and read it as today at five o'clock.

        The model emits a bare clock time routinely -- it is answering "this
        evening", and the hour is the only part it was told. Pydantic rejected
        it, the registry returned `invalid_arguments`, and the agent reported
        the refusal to the caller as **"there are no evening appointments"**.
        That sentence is false, it is about the agent's own malformed call
        rather than the clinic's day, and the caller has no way to tell.

        The same class of leniency as `_clinic_local` in `scheduling.py`, which
        attaches the clinic's zone to a naive datetime for the same reason: the
        model was told every time is clinic-local, so that is what it means.
        This produces the naive value that function then zones.

        The date comes from UTC today. Within OPD hours the clinic's date and
        UTC's agree for `Asia/Kolkata`; they diverge only before about half
        past five in the morning local time, when the clinic is shut and there
        is nothing to find either way. A tenant far enough west for that to
        bite needs the date resolved against `TenantConfig.timezone` instead --
        and a `clinic_id` is not in scope here, which is the honest reason this
        is a boundary convenience rather than a general date parser.
        """
        if not isinstance(v, str):
            return v
        text = v.strip()
        if not _BARE_CLOCK.fullmatch(text):
            return v
        return f"{datetime.now(UTC).date().isoformat()}T{text}"


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
    include_past: bool = False
    """Deliberately takes NO msisdn.

    Reading a patient's bookings is reading their health data. When the model
    supplied the number, this tool was an enumeration oracle -- "does
    +9198xxxxxxx have appointments at this clinic" answerable for any number,
    autonomously, at any point in the call. The docstring here used to claim
    `identity_verified: Literal[True]` prevented exactly that. It could not:
    the model set the flag itself and the only legal value was True.

    The subject now comes from `ToolContext.verified_msisdn`. Enumeration is
    not blocked, it is unsayable -- there is no field in which to name someone
    else."""


class AppointmentOut(Strict):
    appointment_id: UUID
    doctor_name: str
    specialty: str
    starts_at: datetime
    status: Literal["confirmed"]


class FindAppointmentsOut(Strict):
    appointments: list[AppointmentOut]


# --------------------------------------------------------------------------
# find_doctors  -- AUTONOMOUS, side-effect free, speculatively prefetchable.
#
# Added because the agent could not answer "I want Dr. Anitha Sundaresan".
# `find_slots` takes a `doctor_id` UUID and NOTHING in the tool surface turned
# a spoken name into one, so a caller naming their own doctor -- the single
# commonest way an appointment call opens -- was told she could not be found,
# however carefully they spelled it. `get_clinic_info(field="doctors")` returns
# the roster as a sentence of prose, which is not something a `doctor_id` can
# be read out of.
#
# PROJECT.md D10 is this shape exactly, and `codeswitch-007` opens with it.
# --------------------------------------------------------------------------


class FindDoctorsIn(Strict):
    name: Annotated[str, Field(max_length=80)] | None = None
    """A name as the CALLER said it, not as the clinic spells it.

    Matched loosely on purpose. Speech recognition mangles Indian names
    routinely -- one live call produced "Anita Sondar", then "Anita Sutarisan",
    for Dr. Anitha Sundaresan -- and a lookup that only accepts the exact
    string is a lookup that fails every time it is most needed. Getting a
    shortlist back and reading it out is what a receptionist does.
    """

    specialty: str | None = None


class DoctorOut(Strict):
    doctor_id: UUID
    full_name: str
    specialty: str


class FindDoctorsOut(Strict):
    doctors: list[DoctorOut]


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
    contact: Literal["caller_ani"] | Msisdn
    """WHICH number the confirmation goes to -- designated, not transcribed.

    `"caller_ani"` means the caller asked for the number they are calling from;
    the server fills in the ANI and the model never handles the digits. Any
    other value is a different number the caller spoke aloud, and is validated
    as one.

    This was a bare `Msisdn` the model had to fill, and it is the second reason
    booking accuracy sat at zero. A caller who says "same number" (`normal-001`,
    `normal-008`, `bad_input-007` all do, because that is how people talk) gives
    the model no digits, and the model has no other way to reach the ANI. Live
    runs show it asking for the number turn after turn and then sending
    `"unknown"`. There was no expressible correct call.

    Splitting designation from digits also narrows the transcription failure:
    the ANI path cannot be misheard, because nothing hears it.

    It does NOT make substitution impossible, and no claim here should suggest
    otherwise. A model can still designate `"caller_ani"` when the caller asked
    for someone else's phone -- `bad_input-008` is written for exactly that lie
    and grades it with `tools_forbidden: confirm_booking`. That case assumed all
    along that the ANI was "right there"; until now it was not reachable at all,
    and the trap was hypothetical. This makes it real."""

    patient_display_name: Annotated[str, Field(min_length=1, max_length=120)]

    booking_for: Literal["self", "someone_else"] = "self"
    """Whose appointment this is.

    Asked because it changes what the other answers mean: when a daughter rings
    for her father, the NAME is his and the NUMBER is hers, and a register that
    conflates the two sends the reminder to the wrong person and files the
    visit under the wrong patient. `ambiguous-003` is about exactly this -- a
    shared household handset -- and it is the normal case here, not an edge."""

    patient_age: Annotated[int, Field(ge=0, le=120)] | None = None
    patient_gender: Literal["female", "male", "other", "not_stated"] | None = None
    """Registration demographics, and deliberately no more than that.

    Age and gender are what a front desk writes on a card before anyone has
    seen a doctor: they route to the right clinic list and they are on every
    paper form in the country. They are NOT clinical -- CLAUDE.md rule 3
    forbids a clinical column, and nothing here records a symptom, a history,
    a medication or a reason for the visit. If a future field seems to need
    one, that is the escalation the rule asks for, not a schema edit.

    Both optional and both refusable. A caller who does not want to say is
    booked anyway; `not_stated` exists so declining is recordable rather than
    indistinguishable from never having been asked."""

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
    idempotency_key: IdempotencyKey
    # Identity is not an argument. The registry refuses this tool unless
    # ctx.identity_verified, and the adapter scopes the appointment lookup to
    # ctx.verified_msisdn -- so an appointment belonging to someone else is not
    # found rather than being found and then rejected.


class RescheduleOut(Strict):
    appointment_id: UUID
    supersedes: UUID
    starts_at: datetime
    undo_deadline: datetime


class CancelIn(Strict):
    appointment_id: UUID
    reason: Annotated[str, Field(max_length=200)]
    idempotency_key: IdempotencyKey
    # See RescheduleIn: identity is server-side, not an argument.


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
