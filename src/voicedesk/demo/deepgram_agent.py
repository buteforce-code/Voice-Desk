"""The Deepgram Voice Agent settings payload, generated from what we already have.

    python -m voicedesk.demo.deepgram_agent            # print the JSON
    python -m voicedesk.demo.deepgram_agent --prompt   # just the system prompt

Paste the JSON into the Voice Agent playground, or send it as the first
`Settings` message on the `wss://agent.deepgram.com/v1/agent/converse` socket.

**Read this before wiring it to a real caller.**

Deepgram Voice Agent runs the whole pipeline: it hears, it reasons, it speaks.
That is what makes it fast and it is also what it costs. Two of this project's
hard rules do not survive the move unchanged:

1. **The clinical guard stops being a control.** `safety/clinical.py` screens
   the agent's words on the way out, AFTER the model has produced them and
   BEFORE anyone hears them. In this architecture the model's text goes
   straight into Deepgram's own text-to-speech; the `ConversationText` event
   arrives at our server at roughly the same moment the caller hears it. The
   guard can log, alert and end the call. It cannot prevent the sentence.
   CLAUDE.md rule 2 says clinical refusal is "enforced by an output-side
   classifier (C13), not by prompt text". Under Voice Agent it is enforced by
   the prompt below, which the same rule calls not a control.

2. **The state machine stops gating the write.** Below, `confirm_booking` is a
   function the model may call whenever it decides to. Our `execute` state and
   its approval token are what make a write mean "the caller said yes, in their
   own words, and code heard them".

3. **The spoken-form renderer never runs.** `voice/speech.py` turns
   "2026-08-29T09:30:00+05:30" into "Saturday, the twenty-ninth of August at
   nine thirty in the morning", strips markup and groups a phone number into
   something a caller can write down. It runs at the synthesis boundary in
   `demo/server._tts`, and in this architecture there is no such boundary in
   our process: the model's text goes straight into Deepgram's own
   text-to-speech. Everything that module does is demoted to the prompt below,
   alongside the other two -- and the same rule applies to all three, that a
   prompt is not a control. Expect timestamps read out as digits on this path.

The first two are recoverable, and the recovery is the same one: **run the
functions client-side.** Deepgram emits `FunctionCallRequest`, our server executes it
through the real `ToolRegistry` -- tier check, identity gate, approval token,
audit row -- and replies with `FunctionCallResponse`. Authorization stays
server-side and the write stays gated. What cannot be recovered that way is the
output-side guard, because by then the words are already audio.

So: this file is for the demo and for measuring latency. `demo/server.py` is the
architecture that keeps the controls, and it is the one a real caller gets.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from voicedesk.prompts import MULTILINGUAL_INVITE, disclosure_line, system_prompt
from voicedesk.state import CallState
from voicedesk.tenants import Tenant, load_tenants
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.scheduling import TenantConfig, register_scheduling_tools

# -- the choices, each with the reason it was made -------------------------

LISTEN_MODEL = "nova-3-medical"
"""Deepgram's medical-tuned transcription.

Chosen over plain `nova-3` for one narrow reason and not the obvious one: this
agent must never discuss a condition, but callers name them constantly --
"sugar", "thyroid", "BP" -- and the difference between hearing "endocrinology"
and hearing "and Docklands" decides whether the call books or fails. The
medical model is better at exactly the vocabulary this agent must recognise and
must not engage with.

`nova-3` handles Hindi; Tamil support is thinner. Verify before promising it.
"""

SPEAK_MODEL = "aura-2-thalia-en"
"""Clear, warm, unhurried. English only -- as is every Aura-2 voice today.

The alternatives worth an A/B: `aura-2-asteria-en` (brighter, younger),
`aura-2-athena-en` (calmer, older), `aura-2-hera-en` (warmer, slower). Pick by
listening to the CONFIRMATION line -- "your appointment is confirmed for
Saturday at five" -- and not the greeting. The greeting is heard once; the
read-back is where a caller decides whether they trust it.

There is no Aura-2 voice for Tamil or Hindi. A demo in either needs a different
provider for `speak`, and the production answer is Sarvam Bulbul (D1).
"""

THINK_MODEL = "gpt-4o-mini"
THINK_TEMPERATURE = 0.2
"""Booking is not a creative task.

The same 0.2 the real pipeline uses, for the same reason: at higher
temperatures the model invents a doctor, or reads back a time it was never
shown, and both are indistinguishable from competence until the patient
arrives. Low temperature is a correctness decision, not a style one.
"""


def build_settings(tenant: Tenant, *, language: str = "en") -> dict[str, Any]:
    """The full `Settings` message.

    The prompt and the functions are read from `prompts.py` and the live tool
    registry rather than retyped here. A second copy of either is a second
    thing to keep in step, and the first time it drifts is the first time the
    voice agent offers a tool that no longer exists.
    """
    return {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "linear16", "sample_rate": 16000},
            "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        },
        "agent": {
            "language": language,
            "listen": {"provider": {"type": "deepgram", "model": LISTEN_MODEL}},
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": THINK_MODEL,
                    "temperature": THINK_TEMPERATURE,
                },
                "prompt": agent_prompt(tenant),
                "functions": functions(tenant),
            },
            "speak": {"provider": {"type": "deepgram", "model": SPEAK_MODEL}},
            "greeting": greeting(tenant),
        },
    }


def greeting(tenant: Tenant, language: str = "en-IN") -> str:
    """First words on the line.

    Disclosure first, unconditionally -- PROJECT.md 1.4, and the DPDP
    requirement for a notice at point of care. Then the offer of help, then the
    one-time invitation to switch language. Said once and never repeated: by
    the second turn the caller's language is known, and repeating it would mean
    telling a Tamil speaker, in English, that Tamil is allowed.
    """
    return f"{disclosure_line(tenant, language)} How can I help you today? {MULTILINGUAL_INVITE}"


def agent_prompt(tenant: Tenant) -> str:
    """The system prompt, plus what this architecture has to say in words.

    The appended section exists only because Voice Agent removes the code that
    would otherwise enforce it. Every line of it is a control somewhere else in
    this repo, demoted to an instruction here. That demotion is the tradeoff,
    written down where the next person will see it.
    """
    base = system_prompt(tenant, state=CallState.INTAKE, language="en-IN")
    return base + """

WHAT THIS ARCHITECTURE CANNOT ENFORCE FOR YOU
- Never say anything clinical: no advice, no interpreting a symptom, no view on
  how urgent something is, no discussion of results, reports, medication or
  diagnoses -- including confirming that a report exists. If a caller raises
  any of it, say plainly that you only handle appointments, and offer the front
  desk. In the primary pipeline a classifier reads every sentence you produce
  before the caller hears it. Here nothing does.
- Choosing a specialty from described symptoms is clinical. A caller who says
  how they feel and asks which doctor to see gets a transfer. Booking a
  specialty the caller NAMES is fine.
- Never call `confirm_booking` until the caller has agreed, out loud, to ONE
  specific time you have read back to them. Not to a list. In the primary
  pipeline this is a state machine and a token you cannot mint.
- Never claim an action succeeded unless a function result says it did.
- Write every number the way it is SAID, because there is no renderer between
  you and the voice on this path. Never emit a timestamp, a slot id, or a
  24-hour clock. "Saturday the twenty-ninth of August at nine thirty in the
  morning", not "2026-08-29T09:30:00+05:30". "Five hundred rupees", not
  "Rs. 500". Read a phone number back in groups of four, three and three. No
  markdown of any kind: asterisks and hyphens are read out as words.
"""


def functions(tenant: Tenant) -> list[dict[str, Any]]:
    """Function declarations, taken from the live registry.

    `client_side: true` on every one of them, and that is the whole reason this
    payload is safe enough to try. Deepgram sends a `FunctionCallRequest`, our
    server runs it through the real `ToolRegistry` -- tier, identity, approval
    token, idempotency, audit row -- and answers with `FunctionCallResponse`.
    A model deciding to call a tool is still not authorization.

    Flip these to server-side and Deepgram calls the endpoint itself, which
    removes the registry from the path entirely. Do not.
    """
    registry = ToolRegistry(_NullAudit())
    register_scheduling_tools(registry, _NullAdapter(), TenantConfig.from_tenant(tenant))
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
            "client_side": True,
        }
        for spec in registry.schema_for_llm()
    ]


class _NullAudit:
    """The registry needs a sink to be constructed. Nothing is invoked here."""

    async def record(self, *args: Any, **kwargs: Any) -> None: ...

    async def find_replay(self, key: str) -> dict[str, Any] | None:
        return None


class _NullAdapter:
    """Ditto. This module reads schemas; it never runs a handler."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} called while only reading tool schemas")


def main() -> int:
    ap = argparse.ArgumentParser(prog="voicedesk.demo.deepgram_agent")
    ap.add_argument("--prompt", action="store_true", help="print only the system prompt")
    ap.add_argument("--tenant", default="meridian")
    args = ap.parse_args()

    from voicedesk.demo.server import REPO

    tenant = load_tenants(REPO / "config" / "tenants")[args.tenant]
    if args.prompt:
        print(agent_prompt(tenant))
        return 0
    print(json.dumps(build_settings(tenant), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
