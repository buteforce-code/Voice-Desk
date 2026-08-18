"""Designated source-of-truth adapter.

For clinics with no scheduling API, this Postgres schema *is* the register,
reconciled to the clinic's own book daily. `HmisAdapter` stays unimplemented
until a real clinic exists — see `base.py`.

Three rules hold in every method below, and each is checked by a test:

  1. **Every statement names `clinic_id` explicitly.** RLS already confines the
     transaction to one tenant, but a policy is one mechanism and a WHERE
     clause is another. Tenant isolation is the property most expensive to get
     wrong here, so it does not rest on a single line of SQL in another file.

  2. **No statement is built by string concatenation.** Every value is a bound
     parameter. There is no code path that accepts SQL from anywhere.

  3. **Nothing is deleted and nothing meaningful is overwritten.** Appointments
     are soft-versioned: a reschedule writes a new row and points the old one at
     it. `undo` is a pointer move, not a reconstruction.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import asyncpg
import structlog

from voicedesk.adapters.base import (
    AppointmentNotFound,
    SchedulingError,
    SlotUnavailable,
)
from voicedesk.tools.schemas import AppointmentOut, SlotOut

log = structlog.get_logger(__name__)

EXPECTED_ROLE = "voicedesk_agent"


def isolation_problems(
    role_name: str, *, is_super: bool, bypasses_rls: bool
) -> list[str]:
    """Reasons the connected role would not be subject to RLS.

    Pulled out of `connect()` so the decision is testable without a database.
    The check itself is worth more than the connection it guards: every policy
    in 0002 is void against a role that bypasses them, and the failure mode is
    silent — correct-looking queries that quietly span tenants.
    """
    problems = []
    if role_name != EXPECTED_ROLE:
        problems.append(f"connected as '{role_name}', expected '{EXPECTED_ROLE}'")
    if is_super:
        problems.append("role is a superuser, so RLS does not apply")
    if bypasses_rls:
        problems.append("role has BYPASSRLS, so RLS does not apply")
    return problems


class PostgresAdapter:
    """Implements `SchedulingAdapter` against the designated-SoT schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- lifecycle -------------------------------------------------------

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 2, max_size: int = 10
    ) -> PostgresAdapter:
        """Open the pool and refuse to start if RLS would not apply.

        A table's owner bypasses row-level security, and so does any role with
        `rolbypassrls`. If the voice service connects as either, every policy in
        0002_rls_policies.sql silently does nothing and the first cross-tenant
        bug is a data breach rather than an empty result set.

        There is no configuration flag to skip this. A deployment that cannot
        connect as the agent role is a deployment with no tenant isolation, and
        it should fail loudly at boot rather than serve calls.
        """
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        if pool is None:  # pragma: no cover - asyncpg returns None only on misuse
            raise SchedulingError("could not create connection pool")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select current_user::text                       as role_name,
                       coalesce(r.rolsuper, false)              as is_super,
                       coalesce(r.rolbypassrls, false)          as bypasses_rls
                  from pg_roles r
                 where r.rolname = current_user
                """
            )

        if row is None:
            raise SchedulingError("could not determine the connected role")

        problems = isolation_problems(
            row["role_name"],
            is_super=row["is_super"],
            bypasses_rls=row["bypasses_rls"],
        )

        if problems:
            await pool.close()
            raise SchedulingError(
                "refusing to start without tenant isolation: " + "; ".join(problems)
            )

        log.info("adapter.connected", role=row["role_name"])
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # -- tenant scoping --------------------------------------------------

    @contextlib.asynccontextmanager
    async def _tenant_tx(self, clinic_id: UUID) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection pinned to one tenant for one transaction.

        `set_config(..., true)` is transaction-local. On a pooled connection
        that is not a detail: session-scoped would leave one clinic's id set
        when the connection is handed to the next call.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "select set_config('app.clinic_id', $1, true)", str(clinic_id)
                )
                yield conn

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
        """Open slots only: not held by another live call, not already booked.

        A slot under an active hold belongs to whoever is on the phone right
        now. Offering it to a second caller produces two people told the same
        time, which the partial unique index would then resolve by failing one
        of them at the confirmation step — the worst possible moment.
        """
        async with self._tenant_tx(clinic_id) as conn:
            rows = await conn.fetch(
                """
                select s.id            as slot_id,
                       s.doctor_id     as doctor_id,
                       d.full_name     as doctor_name,
                       d.specialty     as specialty,
                       s.starts_at     as starts_at,
                       s.ends_at       as ends_at
                  from opd_slots s
                  join doctors d
                    on d.id = s.doctor_id
                   and d.clinic_id = s.clinic_id
                 where s.clinic_id = $1
                   and d.active
                   and s.starts_at >= coalesce($2, now())
                   and ($3::timestamptz is null or s.starts_at <= $3)
                   and ($4::text is null or d.specialty = $4)
                   and ($5::uuid is null or s.doctor_id = $5)
                   and (s.held_until is null or s.held_until < now())
                   and not exists (
                         select 1
                           from appointments a
                          where a.clinic_id = s.clinic_id
                            and a.slot_id = s.id
                            and a.status = 'confirmed'
                       )
                 order by s.starts_at
                 limit $6
                """,
                clinic_id,
                earliest,
                latest,
                specialty,
                doctor_id,
                limit,
            )

        return [SlotOut(**dict(r)) for r in rows]

    async def find_appointments(
        self, clinic_id: UUID, *, patient_msisdn: str, include_past: bool
    ) -> list[AppointmentOut]:
        """Confirmed bookings for exactly one msisdn in exactly one clinic.

        Identity is enforced at the tool boundary (`identity_verified:
        Literal[True]`). This method's job is to be un-abusable even so: it
        takes one number, matches it exactly, and returns nothing for a number
        it does not hold. There is no prefix match and no wildcard, because an
        enumeration oracle over "does this number have appointments here" is
        itself a health-data leak.
        """
        async with self._tenant_tx(clinic_id) as conn:
            rows = await conn.fetch(
                """
                select a.id          as appointment_id,
                       d.full_name   as doctor_name,
                       d.specialty   as specialty,
                       s.starts_at   as starts_at,
                       a.status      as status
                  from appointments a
                  join patients  p on p.id = a.patient_id  and p.clinic_id = a.clinic_id
                  join doctors   d on d.id = a.doctor_id   and d.clinic_id = a.clinic_id
                  join opd_slots s on s.id = a.slot_id     and s.clinic_id = a.clinic_id
                 where a.clinic_id = $1
                   and p.msisdn = $2
                   and a.status = 'confirmed'
                   and ($3 or s.starts_at >= now())
                 order by s.starts_at
                """,
                clinic_id,
                patient_msisdn,
                include_past,
            )

        return [AppointmentOut(**dict(r)) for r in rows]

    # -- holds -----------------------------------------------------------

    async def hold_slot(
        self, clinic_id: UUID, slot_id: UUID, call_id: UUID, ttl_seconds: int
    ) -> datetime:
        """Take a short TTL reservation. Never described to the caller as a
        booking — it self-expires and is released on abandoned/failed."""
        async with self._tenant_tx(clinic_id) as conn:
            held_until = await conn.fetchval(
                """
                update opd_slots
                   set held_until   = now() + make_interval(secs => $3::int),
                       held_by_call = $4
                 where clinic_id = $1
                   and id = $2
                   and (held_until is null or held_until < now() or held_by_call = $4)
                   and not exists (
                         select 1
                           from appointments a
                          where a.clinic_id = opd_slots.clinic_id
                            and a.slot_id = opd_slots.id
                            and a.status = 'confirmed'
                       )
                returning held_until
                """,
                clinic_id,
                slot_id,
                ttl_seconds,
                call_id,
            )

        if held_until is None:
            raise SlotUnavailable("slot is held by another call or already booked")
        return held_until

    async def release_hold(self, clinic_id: UUID, call_id: UUID) -> None:
        """A dropped call must not park a slot until its TTL expires."""
        async with self._tenant_tx(clinic_id) as conn:
            await conn.execute(
                """
                update opd_slots
                   set held_until = null, held_by_call = null
                 where clinic_id = $1 and held_by_call = $2
                """,
                clinic_id,
                call_id,
            )

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
        """Returns (appointment_id, starts_at, doctor_name).

        The partial unique index `appointments_one_live_per_slot` decides the
        race, not this code: a concurrent winner turns the loser's insert into
        a constraint violation, which becomes `SlotUnavailable` and a re-offer.
        The alternative — checking then inserting — has a window between the
        two, and that window is a double-booked patient.
        """
        async with self._tenant_tx(clinic_id) as conn:
            slot = await conn.fetchrow(
                """
                select s.id, s.starts_at, s.doctor_id, d.full_name as doctor_name
                  from opd_slots s
                  join doctors d
                    on d.id = s.doctor_id
                   and d.clinic_id = s.clinic_id
                 where s.clinic_id = $1
                   and s.id = $2
                   and s.starts_at > now()
                   and d.active
                """,
                clinic_id,
                slot_id,
            )
            if slot is None:
                raise SlotUnavailable("slot does not exist, has passed, or doctor inactive")

            # A first-time caller has no row. coalesce keeps whatever name the
            # clinic already holds — ASR is not authoritative over the register.
            patient_id = await conn.fetchval(
                """
                insert into patients (clinic_id, msisdn, display_name)
                values ($1, $2, $3)
                on conflict (clinic_id, msisdn) do update
                   set display_name = coalesce(patients.display_name, excluded.display_name)
                returning id
                """,
                clinic_id,
                patient_msisdn,
                patient_display_name,
            )

            try:
                appointment_id = await conn.fetchval(
                    """
                    insert into appointments
                        (clinic_id, patient_id, doctor_id, slot_id,
                         status, version, created_by_call)
                    values ($1, $2, $3, $4, 'confirmed', 1, $5)
                    returning id
                    """,
                    clinic_id,
                    patient_id,
                    slot["doctor_id"],
                    slot_id,
                    call_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise SlotUnavailable("slot was booked by another caller") from exc

        return appointment_id, slot["starts_at"], slot["doctor_name"]

    async def reschedule(
        self, clinic_id: UUID, *, appointment_id: UUID, new_slot_id: UUID, call_id: UUID
    ) -> tuple[UUID, datetime]:
        """Soft-versioned move. Writes a new row, points the old one at it.

        The old row is demoted *before* the new one is inserted. Both orderings
        look equivalent until the caller reschedules onto the slot they already
        hold, at which point insert-first trips the one-live-per-slot index
        against the row being replaced.
        """
        async with self._tenant_tx(clinic_id) as conn:
            old = await conn.fetchrow(
                """
                select id, patient_id, version
                  from appointments
                 where clinic_id = $1 and id = $2 and status = 'confirmed'
                """,
                clinic_id,
                appointment_id,
            )
            if old is None:
                raise AppointmentNotFound("no confirmed appointment with that id")

            slot = await conn.fetchrow(
                """
                select s.id, s.starts_at, s.doctor_id
                  from opd_slots s
                  join doctors d
                    on d.id = s.doctor_id
                   and d.clinic_id = s.clinic_id
                 where s.clinic_id = $1
                   and s.id = $2
                   and s.starts_at > now()
                   and d.active
                """,
                clinic_id,
                new_slot_id,
            )
            if slot is None:
                raise SlotUnavailable("target slot does not exist, has passed, or doctor inactive")

            await conn.execute(
                """
                update appointments set status = 'superseded'
                 where clinic_id = $1 and id = $2
                """,
                clinic_id,
                appointment_id,
            )

            try:
                new_id = await conn.fetchval(
                    """
                    insert into appointments
                        (clinic_id, patient_id, doctor_id, slot_id,
                         status, version, supersedes, created_by_call)
                    values ($1, $2, $3, $4, 'confirmed', $5, $6, $7)
                    returning id
                    """,
                    clinic_id,
                    old["patient_id"],
                    slot["doctor_id"],
                    new_slot_id,
                    old["version"] + 1,
                    appointment_id,
                    call_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise SlotUnavailable("target slot was booked by another caller") from exc

            await conn.execute(
                """
                update appointments set superseded_by = $3
                 where clinic_id = $1 and id = $2
                """,
                clinic_id,
                appointment_id,
                new_id,
            )

        return new_id, slot["starts_at"]

    async def cancel(
        self, clinic_id: UUID, *, appointment_id: UUID, reason: str, call_id: UUID
    ) -> datetime:
        """Soft-cancel. The row stays; only its status and timestamps move."""
        async with self._tenant_tx(clinic_id) as conn:
            cancelled_at = await conn.fetchval(
                """
                update appointments
                   set status        = 'cancelled',
                       cancel_reason = $3,
                       cancelled_at  = now()
                 where clinic_id = $1 and id = $2 and status = 'confirmed'
                returning cancelled_at
                """,
                clinic_id,
                appointment_id,
                reason,
            )

        if cancelled_at is None:
            raise AppointmentNotFound("no confirmed appointment with that id")
        return cancelled_at

    async def undo(self, clinic_id: UUID, *, appointment_id: UUID) -> None:
        """Reverse one executed action within its undo window.

        `appointment_id` is whatever the tool returned, so the row's own shape
        says which of the three writes is being undone:

          * cancelled, no predecessor  -> a cancel. Restore it in place.
          * has a predecessor          -> a reschedule. Demote this row and
                                          bring the predecessor back.
          * confirmed, no predecessor  -> a fresh booking. Undoing it can only
                                          mean cancelling it.

        Because every version is a row that was never overwritten, all three
        branches are pointer moves. Nothing is reconstructed from an audit log,
        which is what makes G3's "undo exists before the action ships" a
        structural claim rather than a promise.
        """
        async with self._tenant_tx(clinic_id) as conn:
            row = await conn.fetchrow(
                """
                select id, status, supersedes
                  from appointments
                 where clinic_id = $1 and id = $2
                """,
                clinic_id,
                appointment_id,
            )
            if row is None:
                raise AppointmentNotFound("no appointment with that id")

            if row["supersedes"] is not None:
                # Demote first: predecessor and successor may share a slot.
                await conn.execute(
                    """
                    update appointments set status = 'superseded', superseded_by = null
                     where clinic_id = $1 and id = $2
                    """,
                    clinic_id,
                    appointment_id,
                )
                await conn.execute(
                    """
                    update appointments
                       set status        = 'confirmed',
                           superseded_by = null,
                           cancelled_at  = null,
                           cancel_reason = null
                     where clinic_id = $1 and id = $2
                    """,
                    clinic_id,
                    row["supersedes"],
                )
                return

            if row["status"] == "cancelled":
                await conn.execute(
                    """
                    update appointments
                       set status        = 'confirmed',
                           cancelled_at  = null,
                           cancel_reason = null
                     where clinic_id = $1 and id = $2
                    """,
                    clinic_id,
                    appointment_id,
                )
                return

            await conn.execute(
                """
                update appointments
                   set status        = 'cancelled',
                       cancelled_at  = now(),
                       cancel_reason = 'undone within the undo window'
                 where clinic_id = $1 and id = $2
                """,
                clinic_id,
                appointment_id,
            )
