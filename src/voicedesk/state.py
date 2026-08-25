"""Call state machine (G3).

`docs/STATE_MACHINE.md` and the `call_state` enum in `0001_init.sql` have
described this since G3. Nothing implemented it, which had a consequence that
only became visible once identity moved server-side (D12): `ToolContext` was
never constructed anywhere in `src/`, so nothing could ever set
`identity_verified`, and the three identity-gated tools were unreachable.

This module is the only thing that builds a `ToolContext`. That is the point --
the model cannot write to a session, so it cannot assert its own state, its own
identity or its own approval.

Two invariants are enforced structurally rather than by review:

  1. **`execute` has exactly one inbound edge, from `approval`.** Asserted over
     the edge table itself, so adding a shortcut is a test failure rather than
     something a reader has to notice.
  2. **At most one repair loop.** `validate -> repair -> validate` once. A
     second attempt goes to `transfer`, because an agent that cannot fix a
     draft in one bounded retry is an agent guessing at what is wrong.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

import structlog

from voicedesk.tools.schemas import ToolContext, normalize_msisdn

log = structlog.get_logger(__name__)

MAX_IDENTITY_ATTEMPTS = 3
MAX_REPAIR_LOOPS = 1


class CallState(str, Enum):
    """Mirrors the `call_state` enum in 0001_init.sql exactly.

    Kept in the same order as the migration. `tests/test_state_machine.py`
    asserts parity, because a Python enum that drifts from the database enum
    fails at INSERT time on a live call.
    """

    INTAKE = "intake"
    IDENTIFY = "identify"
    RESEARCH = "research"
    DRAFT = "draft"
    VALIDATE = "validate"
    REPAIR = "repair"
    APPROVAL = "approval"
    EXECUTE = "execute"
    AUDIT = "audit"
    WRAP = "wrap"
    TRANSFER = "transfer"
    ABANDONED = "abandoned"
    FAILED = "failed"
    REFUSED = "refused"


#: Reachable from anywhere. Transfer is the safe default and is never blocked.
LATERAL: frozenset[CallState] = frozenset({
    CallState.TRANSFER,
    CallState.ABANDONED,
    CallState.FAILED,
    CallState.REFUSED,
})

#: Nothing leaves these.
TERMINAL: frozenset[CallState] = LATERAL | {CallState.WRAP}

#: Forward edges only. Laterals are added on top for every non-terminal state.
FORWARD: dict[CallState, frozenset[CallState]] = {
    CallState.INTAKE: frozenset({CallState.IDENTIFY}),
    CallState.IDENTIFY: frozenset({CallState.RESEARCH}),
    CallState.RESEARCH: frozenset({CallState.DRAFT}),
    CallState.DRAFT: frozenset({CallState.VALIDATE}),
    CallState.VALIDATE: frozenset({CallState.APPROVAL, CallState.REPAIR}),
    CallState.REPAIR: frozenset({CallState.VALIDATE}),
    CallState.APPROVAL: frozenset({CallState.EXECUTE}),
    CallState.EXECUTE: frozenset({CallState.AUDIT}),
    CallState.AUDIT: frozenset({CallState.WRAP}),
    CallState.WRAP: frozenset(),
    CallState.TRANSFER: frozenset(),
    CallState.ABANDONED: frozenset(),
    CallState.FAILED: frozenset(),
    CallState.REFUSED: frozenset(),
}


def allowed_from(state: CallState) -> frozenset[CallState]:
    if state in TERMINAL:
        return frozenset()
    return FORWARD[state] | LATERAL


def inbound_edges(target: CallState) -> frozenset[CallState]:
    """Every state with a forward edge into `target`. Laterals excluded --
    they are deliberately reachable from everywhere."""
    return frozenset(s for s, outs in FORWARD.items() if target in outs)


class StateError(RuntimeError):
    """An illegal transition. Never recovered from silently."""


class IdentityExhausted(StateError):
    """Three failed challenges. The call goes to a human."""


@dataclass(frozen=True)
class VersionStamp:
    """What was in force for a transition.

    `call_state_transitions` requires these on every row. Without them a
    regression cannot be attributed to a prompt change rather than a model
    change, which is the whole reason G7 asks for per-run version stamping.
    """

    prompt_version: str
    model_version: str
    tool_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    """One append-only row. A run is replayable from these alone."""

    from_state: CallState | None
    to_state: CallState
    reason: str
    at: datetime
    versions: VersionStamp


@dataclass
class CallSession:
    """Server-side truth for one inbound call.

    Mutable by design -- it is the thing that changes as a call proceeds -- but
    the model never touches it. It reaches the model only as an immutable
    `ToolContext` built by `tool_context()`.
    """

    clinic_id: UUID
    call_id: UUID
    trace_id: str
    versions: VersionStamp
    dry_run: bool = True

    state: CallState = CallState.INTAKE
    ani: str | None = None
    """The number this call arrived on, set by the telephony leg at answer.

    A field rather than something bolted on with `setattr`, which is how the
    harness supplied it while the agent fell back to a hardcoded demo
    number. A fallback that looks like a real Indian mobile is the wrong shape
    of default for a value that ends up in a patient record."""
    identity_verified: bool = False
    verified_msisdn: str | None = None
    identity_attempts: int = 0
    repair_loops: int = 0
    consent_captured: bool = False
    _approval_token: str | None = None
    transitions: list[Transition] = field(default_factory=list)

    # -- transitions ------------------------------------------------------

    def transition_to(self, target: CallState, reason: str) -> Transition:
        """Move, or refuse and say why.

        `reason` is required and never defaulted: `call_state_transitions.reason`
        is NOT NULL, and a replay of a call where every reason reads "next" is
        not a replay.
        """
        if not reason.strip():
            raise StateError("a transition needs a reason")

        if self.state in TERMINAL:
            raise StateError(
                f"{self.state.value} is terminal; cannot move to {target.value}"
            )

        if target not in allowed_from(self.state):
            raise StateError(
                f"no edge {self.state.value} -> {target.value}. "
                f"Allowed: {sorted(s.value for s in allowed_from(self.state))}"
            )

        if target is CallState.REPAIR:
            if self.repair_loops >= MAX_REPAIR_LOOPS:
                raise StateError(
                    "repair budget exhausted; transfer instead. An agent that "
                    "cannot fix a draft in one bounded retry is guessing."
                )
            self.repair_loops += 1

        # The token exists only for the approval -> execute window. Minting it
        # on entry and clearing it on exit means a token cannot be carried into
        # a later turn and reused.
        if target is CallState.APPROVAL:
            self._approval_token = secrets.token_urlsafe(32)
        elif self.state is CallState.EXECUTE:
            self._approval_token = None

        transition = Transition(
            from_state=self.state,
            to_state=target,
            reason=reason,
            at=datetime.now(UTC),
            versions=self.versions,
        )
        self.transitions.append(transition)
        self.state = target

        log.info(
            "call.transition",
            trace_id=self.trace_id,
            **{"from": transition.from_state.value if transition.from_state else None},
            to=target.value,
            reason=reason,
        )
        return transition

    def transfer(self, reason: str) -> Transition:
        """Always permitted, from any non-terminal state. C10.

        Given its own method because it is the safe default: making the caller
        of a state machine remember that transfer is legal from everywhere is
        how it ends up conditional.

        **Idempotent.** Transferring a call that is already transferring is a
        no-op, not an error. The eval suite found the missing case: the model
        called `transfer_to_human`, then kept calling tools until the round
        budget ran out, and `_run_model_rounds` transferred again on the way
        out. `transition_to` refuses any move out of a terminal state, so it
        raised `StateError` -- which propagated through `Agent.turn` and killed
        the call. In production that is a dropped line on a caller who was one
        second from reaching a person.

        Two call sites had already grown their own `if state is not TRANSFER`
        guard, which is precisely the shape the docstring above warns about.
        The third forgot, and forgetting is what the method exists to prevent.

        **The guard covers every terminal state, not just `transfer`.** It read
        `if self.state is CallState.TRANSFER` for exactly as long as it took
        bookings to start succeeding. The second baseline run put a call
        through `confirm_booking` -> `audit` -> `wrap`, whereupon the model --
        still inside the same turn -- called `transfer_to_human`, which is
        AUTONOMOUS and never blocked, and this method raised `StateError` from
        `wrap`. The exception propagated through `Agent.turn` and killed the
        call **one instruction after the appointment was written.** The row is
        in the register, the caller hears nothing, the line drops. It looks
        clean in the database and broken to the human, which is the worst pair
        of properties a failure can have.

        Narrowing the guard to one terminal state was a guess that the only
        way to arrive here twice was via `transfer`. `wrap` is the other way,
        and it was unreachable when the guard was written because nothing had
        ever booked.
        """
        if self.state in TERMINAL:
            log.warning(
                "call.transfer_after_end",
                trace_id=self.trace_id,
                state=self.state.value,
                reason=reason,
            )
            return self.transitions[-1]
        return self.transition_to(CallState.TRANSFER, reason)

    # -- identity ---------------------------------------------------------

    def verify_identity(self, msisdn: str) -> None:
        """Record a passed DOB challenge. The only way identity is ever set.

        Callable only from `identify`. Verifying later would mean the challenge
        happened after a lookup it was supposed to gate.
        """
        if self.state is not CallState.IDENTIFY:
            raise StateError(
                f"identity can only be established in 'identify', not "
                f"'{self.state.value}'"
            )
        self.identity_verified = True
        self.verified_msisdn = normalize_msisdn(msisdn)
        log.info("call.identity_verified", trace_id=self.trace_id)

    def fail_identity_attempt(self) -> int:
        """Count a failed challenge. Three exhausts it and the call transfers.

        Returns attempts remaining, so the caller can be told how many tries
        are left rather than being cut off without warning.
        """
        if self.state is not CallState.IDENTIFY:
            raise StateError("identity attempts only count in 'identify'")

        self.identity_attempts += 1
        remaining = MAX_IDENTITY_ATTEMPTS - self.identity_attempts
        if remaining <= 0:
            self.transfer("identity challenge failed three times")
            raise IdentityExhausted(
                "three failed identity attempts; transferred to a human"
            )
        return remaining

    # -- the model-facing view -------------------------------------------

    def tool_context(self, *, speculative: bool = False) -> ToolContext:
        """The immutable snapshot handed to the tool registry.

        Every field the registry authorizes on originates here, and this object
        is frozen, so a tool handler cannot promote itself. The model has no
        path to a `CallSession` at all.
        """
        return ToolContext(
            clinic_id=self.clinic_id,
            call_id=self.call_id,
            trace_id=self.trace_id,
            state=self.state.value,
            dry_run=self.dry_run,
            approval_token=self._approval_token,
            speculative=speculative,
            identity_verified=self.identity_verified,
            verified_msisdn=self.verified_msisdn,
            caller_msisdn=self.ani,
        )

    # -- introspection ----------------------------------------------------

    @property
    def has_approval_token(self) -> bool:
        return self._approval_token is not None

    def history(self) -> tuple[str, ...]:
        """States visited, in order. For the dashboard's full-history panel."""
        if not self.transitions:
            return (CallState.INTAKE.value,)
        first = self.transitions[0].from_state
        head = (first.value,) if first else ()
        return head + tuple(t.to_state.value for t in self.transitions)


def _assert_execute_has_one_inbound_edge() -> None:
    """Import-time guard on the invariant the whole design rests on.

    docs/STATE_MACHINE.md calls it "the single most important invariant in the
    system". A test asserts it too, but a test can be skipped and this cannot:
    a shortcut into `execute` stops the process from starting.
    """
    sources = inbound_edges(CallState.EXECUTE)
    if sources != frozenset({CallState.APPROVAL}):
        raise AssertionError(
            f"execute must be reachable only from approval, found: "
            f"{sorted(s.value for s in sources)}"
        )
    if CallState.EXECUTE in LATERAL:
        raise AssertionError("execute must not be laterally reachable")


_assert_execute_has_one_inbound_edge()
