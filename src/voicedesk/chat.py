"""Talk to the agent from a terminal. The full dataflow, one key.

    python -m voicedesk.chat

Real Gemini, real tool calling, real state machine, real clinical guard, real
audit trail. Everything except speech and telephony -- you type instead of
talking, and the agent types back instead of being spoken by Bulbul.

That is the whole difference between this and a live call: STT and TTS sit on
either end of exactly this loop. Which is also why it is the right place to
find problems -- a booking bug found here costs a keystroke to reproduce, and
the same bug found over a phone line costs a call.

Needs `GOOGLE_AI_API_KEY` in `.env`. Nothing else: `InMemoryAdapter` keeps
Postgres off the critical path, and no telephony account is involved.

    --trace   show tool calls, state transitions and guard verdicts per turn
    --audit   dump the audit log on exit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import structlog

from voicedesk.adapters.memory import InMemoryAdapter
from voicedesk.agent import Agent
from voicedesk.audit import InMemoryAudit
from voicedesk.config import ConfigError, Settings
from voicedesk.llm import build_from_settings
from voicedesk.state import CallSession, VersionStamp
from voicedesk.tenants import load_tenants
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools

REPO = Path(__file__).parent.parent.parent

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, BLUE, GREY = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[90m",
)


def missing_key_notice() -> int:
    print(f"\n{YELLOW}GOOGLE_AI_API_KEY is not set.{RESET}\n")
    print("This shell runs the real model. Without a key there is nothing to run,")
    print("and a stubbed model here would produce a transcript that means nothing.\n")
    print(f"  {DIM}1.{RESET} cp .env.example .env")
    print(f"  {DIM}2.{RESET} put your Google AI Studio key in GOOGLE_AI_API_KEY")
    print(f"  {DIM}3.{RESET} python -m voicedesk.chat\n")
    print(f"{DIM}Keep GOOGLE_GENAI_USE_VERTEXAI=false — config.py refuses to start")
    print(f"otherwise, and Vertex is a standing org-wide block.{RESET}\n")
    print(f"To see the controls without any key at all: {BOLD}python -m voicedesk.console{RESET}\n")
    return 2


async def run(show_trace: bool, show_audit: bool) -> int:
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"{RED}Configuration refused:{RESET} {exc}")
        return 2

    model = build_from_settings(settings)
    if model is None:
        return missing_key_notice()

    tenant = load_tenants(REPO / "config" / "tenants")["meridian"]
    adapter = InMemoryAdapter.seeded(tenant)
    audit = InMemoryAudit()
    registry = ToolRegistry(audit)
    register_scheduling_tools(registry, adapter, TenantConfig.from_tenant(tenant))

    session = CallSession(
        clinic_id=tenant.clinic_id,
        call_id=uuid4(),
        trace_id=f"chat-{uuid4().hex[:8]}",
        versions=VersionStamp("prompt-2026-08-21", settings.llm_model),
        dry_run=False,
    )
    agent = Agent(
        tenant=tenant,
        session=session,
        registry=registry,
        model=model,
        audit=audit,
        language="en-IN",
    )

    print(f"\n{BOLD}{tenant.display_name}{RESET} {DIM}— {settings.gemini_model}, "
          f"{len(adapter.slots)} slots seeded, dry_run={session.dry_run}{RESET}")
    print(f"{DIM}type as the caller. ctrl-c or 'bye' to hang up.{RESET}\n")

    print(f"{GREEN}agent ›{RESET} {agent.open()}")

    while True:
        try:
            # Off the event loop. Blocking here would stall everything the
            # loop is meant to overlap -- and once STT replaces this prompt,
            # that is audio arriving while we wait.
            caller_text = (
                await asyncio.to_thread(input, f"{BLUE}you   ›{RESET} ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not caller_text:
            continue
        if caller_text.lower() in {"bye", "exit", "quit"}:
            break

        try:
            trace = await agent.turn(caller_text)
        except Exception as exc:  # noqa: BLE001 - a shell should not traceback at a caller
            print(f"{RED}error:{RESET} {exc}")
            continue

        print(f"{GREEN}agent ›{RESET} {trace.spoken_text}")

        if show_trace:
            for name, code in trace.tool_calls:
                colour = GREEN if code == "ok" else RED
                print(f"        {GREY}tool{RESET} {name} {colour}{code}{RESET}")
            if trace.state_before != trace.state_after:
                print(f"        {GREY}state{RESET} {trace.state_before} → {trace.state_after}")
            if trace.clinical_blocked:
                print(f"        {RED}clinical guard blocked{RESET} "
                      f"{','.join(trace.clinical_categories)}")

        if session.state.value in {"transfer", "wrap", "abandoned", "failed"}:
            print(f"\n{DIM}call ended in state: {session.state.value}{RESET}")
            break

    print(f"\n{DIM}states: {' → '.join(session.history())}{RESET}")

    if show_audit:
        print(f"\n{BOLD}audit — {len(audit.rows)} rows{RESET}")
        for row in audit.rows:
            colour = GREEN if row.result == "success" else RED
            reason = f"  {DIM}{row.rejection_reason}{RESET}" if row.rejection_reason else ""
            print(f"  {colour}{row.result:<9}{RESET} {row.tool_name:<24}{reason}")

    booked = [a for a in adapter.appointments.values() if a.status == "confirmed"]
    if booked:
        print(f"\n{BOLD}appointments now in the register:{RESET}")
        for appointment in booked:
            print(f"  {appointment.starts_at:%Y-%m-%d %H:%M}  "
                  f"{appointment.doctor_name}  ({appointment.specialty})")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="voicedesk.chat")
    parser.add_argument("--trace", action="store_true", help="show tools and states")
    parser.add_argument("--audit", action="store_true", help="dump the audit log")
    args = parser.parse_args()
    return asyncio.run(run(args.trace, args.audit))


if __name__ == "__main__":
    sys.exit(main())
