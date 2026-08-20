"""Booking semantics: double-booking, slot validity, soft versioning, undo.

Covers three modules planned separately in tests/README.md —
`test_double_booking.py`, `test_slot_validity.py` and `test_undo.py` — because
they are one subject. All three are about what a booking IS, and splitting them
would mean three files sharing one fixture and one set of invariants.

These run against `InMemoryAdapter`. That is worth being explicit about: a fake
more permissive than the real thing teaches the layers above it the wrong
lesson, and the G5 baseline would then encode that lesson. So the assertions
here are written against the *semantics* `PostgresAdapter` enforces in SQL —
the partial unique index, the msisdn join, soft versioning — and several tests
below check the SQL and the fake agree rather than testing the fake alone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import OTHER_MSISDN, REPO_ROOT, VERIFIED_MSISDN, migration_sql

from voicedesk.adapters.base import AppointmentNotFound, SlotUnavailable
from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.tenants import load_tenants

POSTGRES_SRC = REPO_ROOT / "src" / "voicedesk" / "adapters" / "postgres.py"


@pytest.fixture
def tenant():
    return load_tenants(REPO_ROOT / "config" / "tenants")["meridian"]


@pytest.fixture
def adapter(tenant) -> InMemoryAdapter:
    return InMemoryAdapter.seeded(tenant)


async def first_slot(adapter: InMemoryAdapter, tenant, specialty="Cardiology"):
    slots = await adapter.find_slots(
        tenant.clinic_id,
        specialty=specialty,
        doctor_id=None,
        earliest=None,
        latest=None,
        limit=1,
    )
    assert slots, "fixture produced no slots"
    return slots[0]


async def book(adapter, tenant, slot, msisdn=VERIFIED_MSISDN, name="Ravi Kumar"):
    return await adapter.confirm_booking(
        tenant.clinic_id,
        slot_id=slot.slot_id,
        patient_msisdn=msisdn,
        patient_display_name=name,
        call_id=uuid4(),
    )


# ==========================================================================
# The fixture itself
# ==========================================================================


def test_slots_are_generated_from_the_tenants_opd_hours(adapter, tenant) -> None:
    """Reading the hours out of config rather than hardcoding them means an
    edited config moves the slots, instead of the fixture quietly disagreeing
    with what the agent tells callers."""
    assert adapter.slots, "no slots seeded"
    hours = tenant.info["opd_hours"]
    assert "9:00 AM" in hours

    starts = {s.starts_at.hour for s in adapter.slots.values()}
    assert min(starts) >= 9
    assert max(starts) < 20


def test_slot_hours_match_the_config_in_the_clinics_timezone(adapter, tenant) -> None:
    """The bug a live call surfaced and no unit test did.

    Slots were built by applying the config's local hour numbers directly to a
    UTC datetime, so "9:00 AM" became 09:00Z -- 14:30 in Asia/Kolkata, an hour
    the clinic is shut. The agent offered it and correctly read it back as
    "2:30 PM IST", which was the only visible sign the two disagreed.

    Every eval case about business hours would have scored against slots that
    could not exist.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tenant.timezone)
    hours = sorted({s.starts_at.astimezone(zone).hour for s in adapter.slots.values()})

    # "Monday to Saturday, 9:00 AM to 1:00 PM and 5:00 PM to 8:00 PM"
    assert hours == [9, 10, 11, 12, 17, 18, 19], (
        f"slot hours {hours} do not match the configured OPD windows"
    )


def test_the_tenant_timezone_is_resolvable() -> None:
    """Windows and slim containers ship no tz database, so ZoneInfo raises
    unless `tzdata` is installed -- and it was not. Nothing in the project
    could resolve the timezone its own tenant config declares."""
    from zoneinfo import ZoneInfo

    assert ZoneInfo("Asia/Kolkata") is not None


def test_no_slots_on_sunday(adapter, tenant) -> None:
    """The demo tenant's config says closed Sundays. Checked in the CLINIC'S
    calendar, not the server's -- near midnight UTC the two disagree about
    which day it is."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tenant.timezone)
    assert not any(
        s.starts_at.astimezone(zone).weekday() == 6 for s in adapter.slots.values()
    )


def test_all_seeded_slots_are_in_the_future(adapter) -> None:
    now = datetime.now(UTC)
    assert all(s.starts_at > now for s in adapter.slots.values())


# ==========================================================================
# Double booking
# ==========================================================================


async def test_two_confirmations_for_one_slot_cannot_both_win(
    adapter, tenant
) -> None:
    """The property the partial unique index exists for. In Postgres the loser
    is a constraint violation; here it is the same refusal, so code above the
    seam behaves identically against either."""
    slot = await first_slot(adapter, tenant)
    await book(adapter, tenant, slot)

    with pytest.raises(SlotUnavailable):
        await book(adapter, tenant, slot, msisdn=OTHER_MSISDN, name="Someone Else")


async def test_the_real_adapter_relies_on_a_constraint_not_a_check(
) -> None:
    """`PostgresAdapter.confirm_booking` must let the database decide the race.

    Checking-then-inserting has a window between the two, and that window is a
    double-booked patient. Asserted against the source because the alternative
    needs two concurrent connections to demonstrate.
    """
    src = POSTGRES_SRC.read_text(encoding="utf-8")
    body = src.split("async def confirm_booking(")[1].split("    async def ")[0]
    assert "UniqueViolationError" in body, (
        "confirm_booking must catch the constraint violation rather than "
        "pre-checking availability"
    )


def test_the_partial_unique_index_exists() -> None:
    sql = migration_sql()
    assert re.search(
        r"create unique index\s+appointments_one_live_per_slot\s+"
        r"on appointments \(slot_id\)\s+where status = 'confirmed'",
        sql,
        re.IGNORECASE,
    ), "the one-live-per-slot index is what makes double-booking impossible"


async def test_a_booked_slot_disappears_from_availability(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    await book(adapter, tenant, slot)

    remaining = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=50,
    )
    assert slot.slot_id not in {s.slot_id for s in remaining}


async def test_a_held_slot_is_not_offered_to_a_second_caller(
    adapter, tenant
) -> None:
    """A slot under an active hold belongs to whoever is on the phone. Offering
    it again produces two people told the same time, and the constraint then
    fails one of them at confirmation — the worst possible moment."""
    slot = await first_slot(adapter, tenant)
    await adapter.hold_slot(tenant.clinic_id, slot.slot_id, uuid4(), 120)

    offered = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=50,
    )
    assert slot.slot_id not in {s.slot_id for s in offered}


async def test_a_released_hold_returns_the_slot(adapter, tenant) -> None:
    """A dropped call must not park a slot until its TTL expires."""
    slot = await first_slot(adapter, tenant)
    call_id = uuid4()
    await adapter.hold_slot(tenant.clinic_id, slot.slot_id, call_id, 120)
    await adapter.release_hold(tenant.clinic_id, call_id)

    offered = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=50,
    )
    assert slot.slot_id in {s.slot_id for s in offered}


async def test_holding_an_already_booked_slot_is_refused(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    await book(adapter, tenant, slot)

    with pytest.raises(SlotUnavailable):
        await adapter.hold_slot(tenant.clinic_id, slot.slot_id, uuid4(), 120)


# ==========================================================================
# Slot validity
# ==========================================================================


async def test_booking_a_nonexistent_slot_is_refused(adapter, tenant) -> None:
    with pytest.raises(SlotUnavailable):
        await adapter.confirm_booking(
            tenant.clinic_id,
            slot_id=uuid4(),
            patient_msisdn=VERIFIED_MSISDN,
            patient_display_name="Ravi Kumar",
            call_id=uuid4(),
        )


async def test_booking_a_past_slot_is_refused(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    adapter.slots[slot.slot_id].starts_at = datetime.now(UTC) - timedelta(hours=1)

    with pytest.raises(SlotUnavailable):
        await book(adapter, tenant, slot)


async def test_a_slot_from_another_clinic_is_refused(adapter, tenant) -> None:
    """Tenant isolation at the adapter, independent of RLS."""
    slot = await first_slot(adapter, tenant)
    with pytest.raises(SlotUnavailable):
        await adapter.confirm_booking(
            uuid4(),  # a different clinic
            slot_id=slot.slot_id,
            patient_msisdn=VERIFIED_MSISDN,
            patient_display_name="Ravi Kumar",
            call_id=uuid4(),
        )


async def test_specialty_filtering_is_case_insensitive(adapter, tenant) -> None:
    lower = await adapter.find_slots(
        tenant.clinic_id, specialty="cardiology", doctor_id=None,
        earliest=None, latest=None, limit=5,
    )
    assert lower, "a caller saying 'cardiology' must not get an empty list"


# ==========================================================================
# Reschedule is soft-versioned
# ==========================================================================


async def test_reschedule_writes_a_new_row_and_keeps_the_old(
    adapter, tenant
) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)

    others = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=5,
    )
    target = others[0]

    new_id, _ = await adapter.reschedule(
        tenant.clinic_id,
        appointment_id=appointment_id,
        new_slot_id=target.slot_id,
        patient_msisdn=VERIFIED_MSISDN,
        call_id=uuid4(),
    )

    assert new_id != appointment_id
    assert adapter.appointments[appointment_id].status == "superseded"
    assert adapter.appointments[appointment_id].superseded_by == new_id
    assert adapter.appointments[new_id].supersedes == appointment_id
    assert adapter.appointments[new_id].version == 2


async def test_rescheduling_someone_elses_appointment_is_not_found(
    adapter, tenant
) -> None:
    """Not "forbidden" — not found. A guessed appointment_id must leak nothing
    about whether it exists, so the failure is indistinguishable from a typo."""
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)

    others = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=5,
    )

    with pytest.raises(AppointmentNotFound):
        await adapter.reschedule(
            tenant.clinic_id,
            appointment_id=appointment_id,
            new_slot_id=others[0].slot_id,
            patient_msisdn=OTHER_MSISDN,
            call_id=uuid4(),
        )


# ==========================================================================
# Cancel is soft
# ==========================================================================


async def test_cancel_keeps_the_row(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)

    await adapter.cancel(
        tenant.clinic_id, appointment_id=appointment_id,
        reason="changed their mind", patient_msisdn=VERIFIED_MSISDN, call_id=uuid4(),
    )

    assert appointment_id in adapter.appointments, "nothing is ever hard-deleted"
    assert adapter.appointments[appointment_id].status == "cancelled"
    assert adapter.appointments[appointment_id].cancelled_at is not None


async def test_cancelling_someone_elses_appointment_is_not_found(
    adapter, tenant
) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)

    with pytest.raises(AppointmentNotFound):
        await adapter.cancel(
            tenant.clinic_id, appointment_id=appointment_id, reason="malice",
            patient_msisdn=OTHER_MSISDN, call_id=uuid4(),
        )


async def test_a_cancelled_slot_becomes_available_again(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)
    await adapter.cancel(
        tenant.clinic_id, appointment_id=appointment_id, reason="changed mind",
        patient_msisdn=VERIFIED_MSISDN, call_id=uuid4(),
    )

    offered = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=50,
    )
    assert slot.slot_id in {s.slot_id for s in offered}


# ==========================================================================
# Undo — G3 requires it to exist before the action ships
# ==========================================================================


async def test_undo_restores_a_cancellation(adapter, tenant) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)
    await adapter.cancel(
        tenant.clinic_id, appointment_id=appointment_id, reason="changed mind",
        patient_msisdn=VERIFIED_MSISDN, call_id=uuid4(),
    )

    await adapter.undo(tenant.clinic_id, appointment_id=appointment_id)

    restored = adapter.appointments[appointment_id]
    assert restored.status == "confirmed"
    assert restored.cancelled_at is None
    assert restored.cancel_reason is None, (
        "a restored appointment carrying the reason it was cancelled reads as "
        "a live cancellation to staff"
    )


async def test_undo_reverses_a_reschedule_to_the_original_slot(
    adapter, tenant
) -> None:
    slot = await first_slot(adapter, tenant)
    appointment_id, original_start, _ = await book(adapter, tenant, slot)

    others = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=5,
    )
    new_id, _ = await adapter.reschedule(
        tenant.clinic_id, appointment_id=appointment_id,
        new_slot_id=others[0].slot_id, patient_msisdn=VERIFIED_MSISDN, call_id=uuid4(),
    )

    await adapter.undo(tenant.clinic_id, appointment_id=new_id)

    assert adapter.appointments[new_id].status == "superseded"
    assert adapter.appointments[appointment_id].status == "confirmed"
    assert adapter.appointments[appointment_id].starts_at == original_start


async def test_undoing_a_fresh_booking_cancels_it(adapter, tenant) -> None:
    """The third branch. A booking with no predecessor cannot be reverted to
    anything, so undoing it can only mean cancelling."""
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)

    await adapter.undo(tenant.clinic_id, appointment_id=appointment_id)
    assert adapter.appointments[appointment_id].status == "cancelled"


async def test_undo_is_a_pointer_move_not_a_reconstruction(
    adapter, tenant
) -> None:
    """G3's claim is structural: because every version is a row that was never
    overwritten, undo moves pointers. If a version were ever discarded, this
    count would drop and undo would have to rebuild from an audit log."""
    slot = await first_slot(adapter, tenant)
    appointment_id, _, _ = await book(adapter, tenant, slot)
    others = await adapter.find_slots(
        tenant.clinic_id, specialty="Cardiology", doctor_id=None,
        earliest=None, latest=None, limit=5,
    )
    new_id, _ = await adapter.reschedule(
        tenant.clinic_id, appointment_id=appointment_id,
        new_slot_id=others[0].slot_id, patient_msisdn=VERIFIED_MSISDN, call_id=uuid4(),
    )
    before = len(adapter.appointments)

    await adapter.undo(tenant.clinic_id, appointment_id=new_id)

    assert len(adapter.appointments) == before, "undo must not create or drop rows"


async def test_undoing_an_unknown_appointment_is_refused(adapter, tenant) -> None:
    with pytest.raises(AppointmentNotFound):
        await adapter.undo(tenant.clinic_id, appointment_id=uuid4())


# ==========================================================================
# The fake and the real adapter must agree
# ==========================================================================


def test_both_adapters_implement_the_same_protocol() -> None:
    """A fake missing a method fails at runtime in whichever code path nobody
    exercised locally."""
    from voicedesk.adapters.base import SchedulingAdapter
    from voicedesk.adapters.postgres import PostgresAdapter

    required = {
        name for name in dir(SchedulingAdapter) if not name.startswith("_")
    }
    for implementation in (InMemoryAdapter, PostgresAdapter):
        missing = required - set(dir(implementation))
        assert not missing, f"{implementation.__name__} is missing {sorted(missing)}"


@pytest.mark.parametrize("method", ["reschedule", "cancel"])
def test_both_adapters_scope_writes_by_the_verified_caller(method: str) -> None:
    """The fake must not be more permissive than the real thing, or the layers
    above it learn the wrong lesson and the eval baseline encodes it."""
    import inspect

    from voicedesk.adapters.postgres import PostgresAdapter

    for implementation in (InMemoryAdapter, PostgresAdapter):
        signature = inspect.signature(getattr(implementation, method))
        assert "patient_msisdn" in signature.parameters, (
            f"{implementation.__name__}.{method} is not scoped by the caller"
        )


def test_the_in_memory_adapter_is_labelled_as_non_production() -> None:
    """It has no persistence, no RLS and no durable audit. Someone will
    eventually reach for it as a quick backend; the module should say no."""
    src = (
        REPO_ROOT / "src" / "voicedesk" / "adapters" / "memory.py"
    ).read_text(encoding="utf-8")
    assert "not for production" in src.lower()
