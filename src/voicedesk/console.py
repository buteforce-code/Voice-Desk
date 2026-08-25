"""Runnable walkthrough of everything built so far. No API keys, no database.

    python -m voicedesk.console

What this proves, end to end and on a bare checkout: the state machine, the
tool registry's authorization, server-side identity, the clinical guard, the
audit trail, and soft-versioned booking with a working undo.

What it deliberately does NOT do is decide anything. There is no model here.
Each scenario drives the machine along a scripted path, which is what makes it
a demonstration of the *controls* rather than of the agent's judgement. Wiring
Gemini in place of the script is the next step, and the first that needs a key.

Read the refusals more closely than the successes. Every scenario after the
first exists because something must not happen.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import structlog

from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.audit import InMemoryAudit
from voicedesk.safety.clinical import guard_agent_turn
from voicedesk.state import CallSession, CallState, VersionStamp
from voicedesk.tenants import load_tenants
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools

CALLER = "+919876543210"
STRANGER = "+919812345678"

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, BLUE = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def _quiet_logs() -> None:
    """The scenarios narrate themselves; structlog would double every line."""
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())


def heading(text: str) -> None:
    print(f"\n{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}  {text}{RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}")


def step(text: str) -> None:
    print(f"  {DIM}·{RESET} {text}")


def caller(text: str) -> None:
    print(f"  {BLUE}caller  ›{RESET} {text}")


def agent(text: str) -> None:
    print(f"  {GREEN}agent   ›{RESET} {text}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def blocked(text: str) -> None:
    print(f"  {RED}✗ BLOCKED{RESET} {text}")


def note(text: str) -> None:
    print(f"    {DIM}{text}{RESET}")


def build() -> tuple[ToolRegistry, InMemoryAdapter, InMemoryAudit, object]:
    tenants = load_tenants(Path(__file__).parent.parent.parent / "config" / "tenants")
    tenant = tenants["meridian"]
    adapter = InMemoryAdapter.seeded(tenant)
    audit = InMemoryAudit()
    registry = ToolRegistry(audit)
    register_scheduling_tools(registry, adapter, TenantConfig.from_tenant(tenant))
    return registry, adapter, audit, tenant


def new_session(tenant: object) -> CallSession:
    return CallSession(
        clinic_id=tenant.clinic_id,  # type: ignore[attr-defined]
        call_id=uuid4(),
        trace_id=f"demo-{uuid4().hex[:8]}",
        versions=VersionStamp("prompt-v1", "scripted-no-model"),
        dry_run=False,
        ani=CALLER,
    )


# ==========================================================================


async def scenario_booking(registry, adapter, audit, tenant) -> None:
    heading("1. A booking, all the way through")
    session = new_session(tenant)

    agent("Meridian Speciality Clinic, this is an automated assistant.")
    note("AI disclosure is unconditional, first turn, every call")
    session.transition_to(CallState.IDENTIFY, "disclosure given, consent captured")

    caller("I'd like to see a cardiologist.")
    session.verify_identity(CALLER)
    ok(f"identity verified against {CALLER}")
    session.transition_to(CallState.RESEARCH, "intent: book")

    result = await registry.invoke(
        "find_slots", {"specialty": "Cardiology", "limit": 3}, session.tool_context()
    )
    slots = result.data["slots"]
    for slot in slots:
        step(f"{slot['starts_at'][:16].replace('T', ' ')} — {slot['doctor_name']}")

    chosen = slots[0]
    session.transition_to(CallState.DRAFT, "slot proposed")

    await registry.invoke(
        "hold_slot", {"slot_id": chosen["slot_id"]}, session.tool_context()
    )
    ok("one slot pinned — a hold is not a booking, and self-expires")
    note("confirm_booking refuses any slot this call does not hold (D25)")

    session.transition_to(CallState.VALIDATE, "draft complete")
    note("nothing has been written yet — draft never writes")

    session.transition_to(CallState.APPROVAL, "caller said yes")
    ok("approval token minted")
    session.transition_to(CallState.EXECUTE, "performing the single authorized write")

    booked = await registry.invoke(
        "confirm_booking",
        {
            "slot_id": chosen["slot_id"],
            "contact": "caller_ani",
            "patient_display_name": "Ravi Kumar",
            "idempotency_key": uuid4().hex,
        },
        session.tool_context(),
    )
    ok(f"booked {booked.data['appointment_id'][:8]}… with {booked.data['doctor_name']}")
    note("the number came from the ANI on the session, never from the model")
    agent(f"Confirmed for {booked.data['starts_at'][:16].replace('T', ' ')}.")

    session.transition_to(CallState.AUDIT, "rows committed")
    session.transition_to(CallState.WRAP, "closing line")
    step(f"states: {' → '.join(session.history())}")
    return booked.data["appointment_id"]


async def scenario_write_outside_execute(registry, tenant) -> None:
    heading("2. The same write, attempted from the wrong state")
    session = new_session(tenant)
    session.transition_to(CallState.IDENTIFY, "consent captured")
    session.verify_identity(CALLER)
    session.transition_to(CallState.RESEARCH, "intent: book")

    caller("Just book it, I don't need to confirm.")
    result = await registry.invoke(
        "confirm_booking",
        {
            "slot_id": str(uuid4()),
            "contact": "caller_ani",
            "patient_display_name": "Ravi Kumar",
            "idempotency_key": uuid4().hex,
        },
        session.tool_context(),
    )
    blocked(f"{result.error_code} — {result.error_message}")
    note("execute has exactly one inbound edge, from approval")
    note("the model cannot mint the token, and the session is not writable by it")


async def scenario_enumeration(registry, tenant) -> None:
    heading("3. Looking up someone else's appointments")
    session = new_session(tenant)
    session.transition_to(CallState.IDENTIFY, "consent captured")

    caller(f"Can you check what appointments {STRANGER} has?")
    result = await registry.invoke(
        "find_appointments", {"include_past": False}, session.tool_context()
    )
    blocked(f"{result.error_code} — no challenge has completed")

    step("and with a verified caller, naming another number:")
    session.verify_identity(CALLER)
    result = await registry.invoke(
        "find_appointments",
        {"include_past": False, "patient_msisdn": STRANGER},
        session.tool_context(),
    )
    blocked(f"{result.error_code} — the tool has no field for a phone number")
    note("enumeration is not refused, it is unsayable — the subject comes from")
    note("ToolContext.verified_msisdn, which only a passed DOB challenge sets")


async def scenario_clinical(tenant) -> None:
    heading("4. A caller asking for medical advice")
    for utterance, language in [
        ("You should stop taking that tablet before the scan.", "en-IN"),
        ("That sounds like a thyroid problem — book endocrinology.", "en-IN"),
        ("शायद यह कोई इंफेक्शन हो सकता है।", "hi-IN"),
        ("It's nothing serious, that can easily wait a month.", "en-IN"),
    ]:
        spoken, verdict = guard_agent_turn(utterance, language=language)
        blocked(f"[{','.join(c.value for c in verdict.categories)}] {utterance}")
        agent(spoken)
    note("the last one is the direction people forget: telling a caller it can")
    note("wait is a clinical judgement, and it sounds like good service")

    grounded = (
        "Please arrive thirty minutes early and bring a photo ID. Do not eat "
        "for four hours before the appointment."
    )
    spoken, verdict = guard_agent_turn(grounded, grounded_spans=(grounded,))
    ok("prep instructions from tenant config pass — same shape, different source")


async def scenario_identity_failure(registry, tenant) -> None:
    heading("5. Three failed identity challenges")
    from voicedesk.state import IdentityExhausted

    session = new_session(tenant)
    session.transition_to(CallState.IDENTIFY, "consent captured")

    for attempt in (1, 2, 3):
        caller(f"attempt {attempt}: a date of birth that doesn't match")
        try:
            remaining = session.fail_identity_attempt()
            step(f"{remaining} attempt(s) remaining")
        except IdentityExhausted:
            blocked("identity exhausted")

    agent("Let me put you through to the front desk.")
    step(f"state: {session.state.value}")
    note("transfer is the safe default, permitted from every state, never blocked")


async def scenario_undo(registry, adapter, tenant, appointment_id: str) -> None:
    heading("6. Undo, as a pointer move")
    from uuid import UUID

    before = await adapter.find_appointments(
        tenant.clinic_id, patient_msisdn=CALLER, include_past=True
    )
    step(f"appointments before cancel: {len(before)}")

    await adapter.cancel(
        tenant.clinic_id,
        appointment_id=UUID(appointment_id),
        reason="caller changed their mind",
        patient_msisdn=CALLER,
        call_id=uuid4(),
    )
    after = await adapter.find_appointments(
        tenant.clinic_id, patient_msisdn=CALLER, include_past=True
    )
    step(f"after cancel: {len(after)} — the row is still there, status changed")

    await adapter.undo(tenant.clinic_id, appointment_id=UUID(appointment_id))
    restored = await adapter.find_appointments(
        tenant.clinic_id, patient_msisdn=CALLER, include_past=True
    )
    ok(f"after undo: {len(restored)} — nothing was reconstructed, a pointer moved")


def scenario_audit(audit: InMemoryAudit) -> None:
    heading("7. What the audit log saw")
    print(f"  {len(audit.rows)} rows, one per ATTEMPT — rejections included\n")
    for row in audit.rows:
        colour = GREEN if row.result == "success" else RED
        reason = f" — {row.rejection_reason}" if row.rejection_reason else ""
        print(f"  {colour}{row.result:<9}{RESET} {row.tool_name:<22}{DIM}{reason}{RESET}")
    note("")
    note("a refusal that leaves no row is a refusal nobody can audit")


async def main() -> int:
    _quiet_logs()
    registry, adapter, audit, tenant = build()

    print(f"\n{BOLD}Voice Desk — local walkthrough{RESET}")
    print(f"{DIM}no API keys, no database, no telephony. scripted, not modelled.{RESET}")
    print(f"{DIM}tenant: {tenant.display_name} ({len(tenant.doctors)} doctors, "
          f"{len(adapter.slots)} slots seeded){RESET}")

    appointment_id = await scenario_booking(registry, adapter, audit, tenant)
    await scenario_write_outside_execute(registry, tenant)
    await scenario_enumeration(registry, tenant)
    await scenario_clinical(tenant)
    await scenario_identity_failure(registry, tenant)
    await scenario_undo(registry, adapter, tenant, appointment_id)
    scenario_audit(audit)

    print(f"\n{YELLOW}Not exercised here:{RESET} the model's decisions. Every path "
          f"above was scripted.")
    print(f"{DIM}Wiring Gemini in place of the script is the next step, and the "
          f"first that needs an API key.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
