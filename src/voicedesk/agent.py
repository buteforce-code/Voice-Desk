"""The turn loop — where everything built so far meets.

One caller utterance in, one agent utterance out. The dataflow:

    caller audio/text
      -> fencing.fence          untrusted input, bounded, stripped, redacted
      -> history
      -> LanguageModel          returns INTENT: text + tool calls it wants
      -> ToolRegistry.invoke    authorization, identity, idempotency, audit
      -> LanguageModel          again, now with results, until it stops asking
      -> clinical guard         output-side, C13/C14
      -> spoken

Two things the model does not get to decide, and both are the reason this file
is an orchestrator rather than a thin wrapper:

**Whether a tool runs.** The model emits `ToolCall`s. `ToolRegistry` decides.
A refusal comes back to the model as a result it has to deal with, which is
also why the prompt tells it refusals are normal.

**Whether the caller agreed.** Confirmation is detected in code from the
caller's own words, not inferred by the model and not asserted in a tool
argument. `approval` is the state that mints the write token, so if the model
could talk the session into it, the token would be worth nothing. This is the
same lesson as D12: a control the model can assert is not a control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

from voicedesk.audit import InMemoryAudit
from voicedesk.llm import LanguageModel, Message, ToolResult
from voicedesk.prompts import disclosure_line, system_prompt
from voicedesk.safety.clinical import guard_agent_turn
from voicedesk.security.fencing import fence, sanitize_utterance
from voicedesk.state import CallSession, CallState, IdentityExhausted
from voicedesk.tenants import Tenant
from voicedesk.tools.registry import ToolRegistry

log = structlog.get_logger(__name__)

MAX_TOOL_ROUNDS = 4
"""Model -> tools -> model cycles per caller turn. A model that has not
finished after four rounds is looping, and a caller is listening to silence."""

# Affirmation, in the three supported languages.
#
# Deliberately narrow, and narrower than it first was. "sure" and bare "right"
# were both in this list until a test caught what that costs:
#
#     "I'm not sure, maybe."
#
# contains "sure", was matched as consent, and booked the appointment. The word
# is unreliable in exactly the position that matters -- "I'm not sure", "are you
# sure?", "sure, whatever" -- and bare "right" is usually a discourse marker
# ("right, so what time?") rather than agreement. Both are gone. The phrase
# "that's right" stays, because it cannot mean anything else.
#
# The asymmetry that governs this list: a missed yes costs one extra turn. A
# false yes books an appointment the caller never agreed to, and they find out
# when they arrive at a clinic.
_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|ok|okay|correct|confirm|confirmed|"
    r"that'?s right|go ahead|book it|please do|sounds good)\b"
    r"|சரி|ஆமா|ஆம்|சரிதான்|பண்ணுங்க"
    r"|हाँ|हां|जी हाँ|ठीक है|कर दीजिए|बुक कर",
    re.IGNORECASE,
)

# Negation DOMINATES: any hedge here means the utterance is not a confirmation,
# even when an affirmative word also appears in it.
_NEGATIVE = re.compile(
    r"\b(no|nope|not|n'?t|cancel that|wrong|wait|hold on|maybe|unsure)\b"
    r"|இல்ல|வேண்டாம்|தப்பு|இருங்க"
    r"|नहीं|मत|गलत|रुक|शायद",
    re.IGNORECASE,
)


@dataclass
class TurnTrace:
    """What happened in one turn. The dashboard renders this; evals score it."""

    caller_text: str
    spoken_text: str
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    """(tool_name, result_code)"""
    state_before: str = ""
    state_after: str = ""
    clinical_blocked: bool = False
    clinical_categories: tuple[str, ...] = ()


@dataclass
class Agent:
    """One live call."""

    tenant: Tenant
    session: CallSession
    registry: ToolRegistry
    model: LanguageModel
    audit: InMemoryAudit
    language: str = "en-IN"

    history: list[Message] = field(default_factory=list)
    traces: list[TurnTrace] = field(default_factory=list)
    pending_write: bool = False
    """True once a concrete proposal is on the table. Only then can a caller's
    yes mean anything -- otherwise an early "sure" would promote a call with
    nothing drafted into the state that writes."""

    # -- call lifecycle ---------------------------------------------------

    def open(self) -> str:
        """First agent turn. Disclosure is unconditional and comes first."""
        line = disclosure_line(self.tenant, self.language)
        greeting = f"{line} How can I help you today?"
        self.history.append(Message(role="agent", text=greeting))
        self.session.transition_to(
            CallState.IDENTIFY, "disclosure given, consent captured"
        )
        return greeting

    async def turn(self, caller_text: str) -> TurnTrace:
        """Handle one caller utterance end to end."""
        trace = TurnTrace(
            caller_text=caller_text,
            spoken_text="",
            state_before=self.session.state.value,
        )

        # Untrusted input. Bounded, structural markers stripped, card and ID
        # numbers redacted -- before it reaches a prompt, a log or a transcript.
        safe_text = sanitize_utterance(caller_text)
        self.history.append(Message(role="caller", text=fence(safe_text)))

        self._advance_on_caller_turn(safe_text)

        spoken = await self._run_model_rounds(trace)

        # Output-side guard. The last thing before anything is spoken.
        spoken, verdict = guard_agent_turn(
            spoken, language=self.language, grounded_spans=self._grounded_spans()
        )
        if verdict.blocked:
            trace.clinical_blocked = True
            trace.clinical_categories = tuple(c.value for c in verdict.categories)
            if self.session.state not in {CallState.TRANSFER}:
                self.session.transfer("clinical request refused")

        self.history.append(Message(role="agent", text=spoken))
        trace.spoken_text = spoken
        trace.state_after = self.session.state.value
        self.traces.append(trace)
        return trace

    # -- the model/tool cycle ---------------------------------------------

    async def _run_model_rounds(self, trace: TurnTrace) -> str:
        spoken = ""
        for _ in range(MAX_TOOL_ROUNDS):
            turn = await self.model.respond(
                system=system_prompt(
                    self.tenant,
                    state=self.session.state,
                    language=self.language,
                    identity_verified=self.session.identity_verified,
                ),
                history=self.history,
                tools=self.registry.schema_for_llm(),
            )
            spoken = turn.text or spoken

            if not turn.wants_tools:
                return spoken

            results: list[ToolResult] = []
            for call in turn.tool_calls:
                result = await self.registry.invoke(
                    call.name, call.args, self.session.tool_context()
                )
                code = "ok" if result.ok else (result.error_code or "error")
                trace.tool_calls.append((call.name, code))

                results.append(
                    ToolResult(
                        name=call.name,
                        payload=(
                            result.data or {}
                            if result.ok
                            else {"error": code, "message": result.error_message}
                        ),
                    )
                )
                self._advance_on_tool(call.name, result.ok)

            self.history.append(Message(role="tool", tool_results=tuple(results)))

        log.warning("agent.tool_rounds_exhausted", trace_id=self.session.trace_id)
        self.session.transfer("model did not settle within the tool-round budget")
        return spoken

    # -- state advancement -------------------------------------------------

    def _advance_on_caller_turn(self, text: str) -> None:
        """Move the machine on what the CALLER said, not on what the model
        claims they said."""
        state = self.session.state

        if state is CallState.IDENTIFY and _looks_like_dob(text):
            # A real deployment checks the DOB against the patient record. The
            # in-memory demo has no patient records to check against, so this
            # accepts a well-formed date. Marked clearly because it is the one
            # place in the system where a control is weaker than it looks.
            try:
                self.session.verify_identity(_caller_msisdn(self.session))
            except IdentityExhausted:  # pragma: no cover - defensive
                return
            self.session.transition_to(CallState.RESEARCH, "identity verified")
            return

        if state is CallState.IDENTIFY and _wants_new_booking(text):
            self.session.transition_to(
                CallState.RESEARCH, "new booking needs no prior identity"
            )
            return

        if state is CallState.DRAFT and self.pending_write:
            # Declining a proposed slot is not a validator failure and not a
            # step backwards -- it is still drafting. The machine is
            # forward-only, and an early version of this method tried
            # validate -> research for a decline, which the edge table
            # correctly refused. The mistake was moving to `validate` on the
            # lookup at all: `validate` is where deterministic checks run
            # against a draft the caller has ALREADY chosen, immediately before
            # approval. Re-proposing never leaves `draft`.
            # Negation dominates. A hedge or refusal anywhere in the
            # utterance means this is not consent, even when an affirmative
            # word also appears -- "no, not Wednesday, but yes to Thursday" is
            # a caller still deciding, and the next turn will say so plainly.
            if _NEGATIVE.search(text):
                return
            if _AFFIRMATIVE.search(text):
                self.session.transition_to(CallState.VALIDATE, "caller chose a slot")
                self.session.transition_to(CallState.APPROVAL, "caller confirmed")
                self.session.transition_to(
                    CallState.EXECUTE, "authorized write may now happen"
                )
                return

    def _advance_on_tool(self, tool_name: str, ok: bool) -> None:
        """Move the machine on what actually happened."""
        state = self.session.state

        if tool_name == "transfer_to_human" and ok:
            if state not in {CallState.TRANSFER}:
                self.session.transfer("agent handed over to a human")
            return

        if not ok:
            return

        if tool_name in {"find_slots", "find_appointments"} and state is CallState.RESEARCH:
            self.session.transition_to(CallState.DRAFT, f"{tool_name} returned")
            self.pending_write = True
            return

        if tool_name in {"confirm_booking", "reschedule_appointment", "cancel_appointment"}:
            self.pending_write = False
            self.session.transition_to(CallState.AUDIT, f"{tool_name} succeeded")
            self.session.transition_to(CallState.WRAP, "confirmed to the caller")

    def _grounded_spans(self) -> tuple[str, ...]:
        """Config values retrieved this call.

        Handed to the clinical guard so tenant prep instructions -- directives
        with a clinical shape, but from a config key with a source -- are not
        mistaken for the model's own advice.
        """
        return tuple(self.tenant.info.values())


# --------------------------------------------------------------------------


_DOB = re.compile(
    r"\b\d{1,2}[/\-. ]\d{1,2}[/\-. ]\d{2,4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\b",
    re.IGNORECASE,
)

_NEW_BOOKING = re.compile(
    r"\bnew\b|\bbook\b|\bappointment\b|\bfirst time\b|\bsee (?:a|the) doctor\b"
    r"|புதுசா|அப்பாயின்ட்மென்ட்|பார்க்கணும்"
    r"|नया|अपॉइंटमेंट|दिखाना",
    re.IGNORECASE,
)


def _looks_like_dob(text: str) -> bool:
    return bool(_DOB.search(text))


def _wants_new_booking(text: str) -> bool:
    return bool(_NEW_BOOKING.search(text))


def _caller_msisdn(session: CallSession) -> str:
    """ANI for this call.

    Carried on the session by the telephony layer. Until that exists the demo
    supplies it explicitly; `verified_msisdn` is never taken from model output
    either way.
    """
    return getattr(session, "ani", None) or "+919876543210"
