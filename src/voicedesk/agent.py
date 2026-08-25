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
from voicedesk.prompts import opening_line, system_prompt
from voicedesk.safety.clinical import guard_agent_turn
from voicedesk.security.fencing import fence, sanitize_utterance
from voicedesk.state import TERMINAL, CallSession, CallState, IdentityExhausted
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
    """True once ONE specific slot is held. Only then can a caller's yes mean
    anything: before a hold there is a list on the table, and agreeing to a
    list does not identify which appointment to make."""

    # -- call lifecycle ---------------------------------------------------

    def open(self) -> str:
        """First agent turn. Disclosure is unconditional and comes first."""
        greeting = opening_line(self.tenant, self.language)
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

        spoken = await self._run_model_rounds(trace, safe_text)

        # The utterance is handed down so consent can be re-read after every
        # tool round.
        #
        # Consent used to be read ONLY before the model moved, which made the
        # order of two events in one turn decide whether a call could ever be
        # booked. A caller who picks a slot and agrees to it in one breath --
        # "that's fine, book it", which is how `normal-001`, `normal-004` and
        # most of the normal slice are written, because it is how people talk
        # -- says their yes BEFORE the `hold_slot` that gives it a referent.
        # `pending_write` was still False, the yes was discarded, and the agent
        # spent the rest of the call asking a question already answered.
        #
        # Nothing about the control is relaxed: it is still the caller's own
        # words, still matched in code, still refused unless exactly one slot
        # is pinned, and negation still dominates. What changed is when the
        # question is asked -- after the hold exists rather than before, which
        # is the only point at which it can be answered honestly.

        # Output-side guard. The last thing before anything is spoken.
        spoken, verdict = guard_agent_turn(
            spoken, language=self.language, grounded_spans=self._grounded_spans()
        )
        if verdict.blocked:
            trace.clinical_blocked = True
            trace.clinical_categories = tuple(c.value for c in verdict.categories)

            # A write that happened must still be told to the caller.
            #
            # The guard replaces the whole utterance, and if the agent wandered
            # into clinical territory in the same breath as confirming a
            # booking, the caller heard only "I can't help with that, let me
            # transfer you" -- while an appointment sat in the register in
            # their name. They arrive, or they do not; either way nobody in the
            # conversation knew it existed.
            #
            # The refusal is still the right thing to say. It is not the ONLY
            # thing that needs saying.
            if any(name in _WRITES and code == "ok" for name, code in trace.tool_calls):
                spoken = f"{_booking_made(self.language)} {spoken}"

            # No state guard here: `transfer` is idempotent. The guard used to
            # live at each call site, and the one place that forgot it crashed
            # the call.
            self.session.transfer("clinical request refused")

        self.history.append(Message(role="agent", text=spoken))
        trace.spoken_text = spoken
        trace.state_after = self.session.state.value
        self.traces.append(trace)
        return trace

    # -- the model/tool cycle ---------------------------------------------

    async def _run_model_rounds(self, trace: TurnTrace, caller_text: str = "") -> str:
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

            # The agent's own decision, recorded before its consequences.
            #
            # This message used to be skipped entirely, and the omission is
            # what held booking accuracy at zero. The model saw its tool
            # RESULTS but never saw that it had asked for them, so a slot_id
            # arrived in the transcript attached to nothing -- no turn in which
            # the agent had chosen to look slots up. On the next utterance it
            # could not tell which of the ids in front of it, if any, it had
            # committed to, and it asked the caller to disambiguate instead of
            # calling `hold_slot`. `normal-001` shows it asking the identical
            # question three times while the caller repeats "book it".
            #
            # `text` is deliberately empty: only ONE utterance per turn reaches
            # the caller, and `turn()` appends it. Recording intermediate model
            # chatter here would put words in the history that were never
            # spoken on the call.
            self.history.append(
                Message(role="agent", tool_calls=turn.tool_calls)
            )

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
                        # Paired to the call that asked for it. An
                        # OpenAI-compatible endpoint rejects a result whose id
                        # matches no preceding call.
                        call_id=call.call_id,
                    )
                )
                self._advance_on_tool(call.name, result.ok)

            self.history.append(Message(role="tool", tool_results=tuple(results)))

            # Re-read consent now, not after the last round. A model that holds
            # a slot asks to write it on the very next round -- that is the
            # natural shape, and it is what the live traces do. Evaluating only
            # once the rounds are over left the session in `draft` for the
            # whole turn, so the write was refused as `not_authorized` and the
            # booking slipped to the following utterance, with a spurious
            # refusal in the transcript on the way.
            self._advance_on_caller_turn(caller_text)

            if self.session.state in TERMINAL:
                # The call ended inside this turn -- booked and wrapped, or
                # handed to a person. No further ROUND runs: this is where the
                # crash came from. A turn that reached `wrap` kept looping, the
                # model called `transfer_to_human`, and `_advance_on_tool`
                # transferred a terminated session, which raised and killed the
                # call one instruction after the appointment was written. The
                # guard in `state.py` stops the crash; this stops the round
                # that provokes it.
                #
                # The call ended inside this turn. The agent must still SPEAK
                # -- a booking confirmed in silence is what the caller hears as
                # a dropped line -- but it must no longer ACT. One more round
                # with no tools offered: it can say "you're booked for Saturday
                # at nine", and there is nothing it can call.
                if not spoken:
                    closing = await self.model.respond(
                        system=system_prompt(
                            self.tenant,
                            state=self.session.state,
                            language=self.language,
                            identity_verified=self.session.identity_verified,
                        ),
                        history=self.history,
                        tools=[],
                    )
                    spoken = closing.text or spoken
                return spoken

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
            ani = self.session.ani
            if not ani:
                # No ANI, no subject. Verifying against a stand-in number would
                # scope every later lookup to a patient who does not exist, and
                # the call would read as verified while looking at nothing.
                self.session.transfer("no ANI on this leg; identity unverifiable")
                return
            try:
                self.session.verify_identity(ani)
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
            self.session.transfer("agent handed over to a human")
            return

        if not ok:
            return

        if tool_name == "find_slots" and state is CallState.IDENTIFY:
            # Searching for a slot IS the research step, and it is only ever
            # done for a NEW booking -- which needs no prior identity.
            #
            # This used to depend entirely on `_wants_new_booking`, a phrase
            # list matching "book", "appointment" and "see a/the doctor". It
            # does not match "I'd like to see a dermatologist", or a
            # cardiologist, or any of the other seven specialties, so an
            # ordinary opening sentence left the call in `identify`. The model
            # then called `find_slots` from the wrong state, the RESEARCH ->
            # DRAFT edge below never fired, `hold_slot` could not set
            # `pending_write`, and the write was refused on a call where the
            # caller had done nothing wrong. The machine ran a full turn behind
            # the conversation.
            #
            # Advancing on the tool rather than the phrasing costs no identity
            # guarantee: `find_appointments` is identity-gated in the registry,
            # server-side, whatever state the call is in. Nothing here can
            # reach an existing patient's data.
            self.session.transition_to(CallState.RESEARCH, "find_slots: a new booking")
            self.session.transition_to(CallState.DRAFT, "find_slots returned")
            return

        if tool_name in {"find_slots", "find_appointments"} and state is CallState.RESEARCH:
            self.session.transition_to(CallState.DRAFT, f"{tool_name} returned")
            return

        # A hold is what pins ONE slot. Until then the caller has been shown a
        # list, and "yes" to a list is not consent to any particular item --
        # the first live run said "Yes, book it" against five options and the
        # session promoted itself into the writing state. This model asked
        # which one; a less careful one books whichever it saw first.
        if tool_name == "hold_slot" and state is CallState.DRAFT:
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


_WRITES = frozenset(
    {"confirm_booking", "reschedule_appointment", "cancel_appointment"}
)

_BOOKING_MADE = {
    "en-IN": "That change has gone through.",
    "ta-IN": "அந்த மாற்றம் பதிவாகிடுச்சு.",
    "hi-IN": "वह बदलाव हो गया है।",
}


def _booking_made(language: str) -> str:
    """Deliberately vague about WHAT changed.

    The guard blocked this turn because the agent said something it should not
    have, and the details it was in the middle of reading out are exactly the
    text under suspicion. Repeating them would defeat the block. Saying that
    something was recorded, and letting the transfer carry the specifics to a
    person, is the honest middle: the caller is not left believing nothing
    happened, and no unreviewed sentence reaches them.
    """
    return _BOOKING_MADE.get(language, _BOOKING_MADE["en-IN"])


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



