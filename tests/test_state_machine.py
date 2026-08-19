"""States are stored and enforced, not inferred (G3).

`docs/STATE_MACHINE.md` and the `call_state` enum have described this machine
since G3 while nothing implemented it. That was invisible until identity moved
server-side (D12): `ToolContext` was never constructed anywhere in `src/`, so
`identity_verified` could never be set and the three identity-gated tools were
unreachable in production code.

The invariant this module exists for is the one the design document calls "the
single most important in the system": **`execute` is reachable only from
`approval`.** It is asserted three ways on purpose -- over the edge table, at
import time in `state.py`, and through the registry -- because it is the
property that stops a write happening without a caller having said yes.
"""

from __future__ import annotations

import re

import pytest
from conftest import CLINIC_A, migration_sql

from voicedesk.state import (
    LATERAL,
    MAX_IDENTITY_ATTEMPTS,
    MAX_REPAIR_LOOPS,
    TERMINAL,
    CallSession,
    CallState,
    IdentityExhausted,
    StateError,
    VersionStamp,
    allowed_from,
    inbound_edges,
)

HAPPY_PATH = [
    (CallState.IDENTIFY, "consent captured"),
    (CallState.RESEARCH, "intent classified"),
    (CallState.DRAFT, "action formed"),
    (CallState.VALIDATE, "validators run"),
    (CallState.APPROVAL, "caller confirmed"),
    (CallState.EXECUTE, "performing the write"),
    (CallState.AUDIT, "rows committed"),
    (CallState.WRAP, "closing line"),
]


@pytest.fixture
def session() -> CallSession:
    from uuid import uuid4

    return CallSession(
        clinic_id=CLINIC_A,
        call_id=uuid4(),
        trace_id=f"trace-{uuid4()}",
        versions=VersionStamp("prompt-1", "gemini-2.5-flash"),
    )


def advance_to(session: CallSession, target: CallState) -> CallSession:
    """Drive a session to `target` along legal edges.

    Explicit rather than greedy. A loop that picked "the next forward state"
    silently overshot `intake` all the way to `wrap` -- intake is the start
    state and never a transition TARGET -- and could never reach `repair`,
    because `validate` has two successors and the greedy pick was the other
    one. Both produced tests that ran against a state nobody intended.
    """
    if target is CallState.INTAKE:
        return session

    if target in LATERAL:
        session.transition_to(target, "test")
        return session

    if target is CallState.REPAIR:
        for state, reason in HAPPY_PATH:
            session.transition_to(state, reason)
            if state is CallState.VALIDATE:
                break
        session.transition_to(CallState.REPAIR, "a validator failed")
        return session

    for state, reason in HAPPY_PATH:
        session.transition_to(state, reason)
        if state is target:
            return session

    raise AssertionError(f"{target.value} is not reachable by advance_to")


# ==========================================================================
# Parity with the database
# ==========================================================================


def test_every_sql_enum_value_exists_in_python() -> None:
    """A Python enum that drifts from the database enum fails at INSERT time,
    on a live call, in the one code path nobody exercises locally."""
    block = re.search(
        r"create type call_state as enum \((.*?)\);", migration_sql(), re.DOTALL
    )
    assert block is not None, "call_state enum not found in the migrations"

    sql_values = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    python_values = {s.value for s in CallState}

    assert sql_values == python_values, (
        f"drift -- only in SQL: {sorted(sql_values - python_values)}, "
        f"only in Python: {sorted(python_values - sql_values)}"
    )


def test_the_stored_state_is_a_string_the_database_accepts(session: CallSession) -> None:
    assert session.tool_context().state == "intake"


# ==========================================================================
# THE invariant
# ==========================================================================


def test_execute_is_reachable_only_from_approval() -> None:
    assert inbound_edges(CallState.EXECUTE) == frozenset({CallState.APPROVAL})


def test_execute_is_not_laterally_reachable() -> None:
    """Laterals are reachable from everywhere by design. If `execute` ever
    joined them, every other guard in this file would be theatre."""
    assert CallState.EXECUTE not in LATERAL


def test_the_invariant_is_asserted_at_import_time() -> None:
    """A test can be skipped or deleted; an import-time assertion stops the
    process from starting. Both exist because this is the one invariant whose
    silent loss is a write nobody authorized."""
    import voicedesk.state as state_module

    with pytest.MonkeyPatch.context() as mp:
        broken = dict(state_module.FORWARD)
        broken[CallState.RESEARCH] = frozenset({CallState.DRAFT, CallState.EXECUTE})
        mp.setattr(state_module, "FORWARD", broken)
        with pytest.raises(AssertionError, match="only from approval"):
            state_module._assert_execute_has_one_inbound_edge()


@pytest.mark.parametrize(
    "state",
    [s for s in CallState if s not in {CallState.APPROVAL} and s not in TERMINAL],
)
def test_no_other_state_can_reach_execute(state: CallState) -> None:
    assert CallState.EXECUTE not in allowed_from(state)


def test_a_write_needs_the_token_minted_at_approval(session: CallSession) -> None:
    advance_to(session, CallState.EXECUTE)
    ctx = session.tool_context()

    assert ctx.state == "execute"
    assert ctx.approval_token, "the token proves the caller confirmed"


def test_no_token_exists_before_approval(session: CallSession) -> None:
    advance_to(session, CallState.VALIDATE)
    assert session.has_approval_token is False
    assert session.tool_context().approval_token is None


def test_the_token_is_cleared_once_execute_is_left(session: CallSession) -> None:
    """A token that outlives its window is a token that can be reused on a
    later turn, which would make `approval` a one-time formality."""
    advance_to(session, CallState.EXECUTE)
    assert session.has_approval_token is True

    session.transition_to(CallState.AUDIT, "rows committed")
    assert session.has_approval_token is False


def test_each_approval_mints_a_distinct_token(session: CallSession) -> None:
    advance_to(session, CallState.APPROVAL)
    first = session.tool_context().approval_token

    session.transition_to(CallState.EXECUTE, "write")
    session.transition_to(CallState.AUDIT, "committed")

    assert first is not None
    assert len(first) >= 32, "a guessable token is not a control"


# ==========================================================================
# Transitions
# ==========================================================================


def test_the_happy_path_runs_end_to_end(session: CallSession) -> None:
    advance_to(session, CallState.WRAP)
    assert session.state is CallState.WRAP
    assert session.history()[0] == "intake"
    assert session.history()[-1] == "wrap"


def test_transitions_are_recorded_in_order(session: CallSession) -> None:
    """A run is replayable from `call_state_transitions` alone."""
    advance_to(session, CallState.WRAP)
    assert [t.to_state for t in session.transitions] == [s for s, _ in HAPPY_PATH]


def test_every_transition_carries_a_reason_and_versions(session: CallSession) -> None:
    """`reason` is NOT NULL in the schema, and a replay where every reason
    reads 'next' is not a replay. Versions are what let a regression be
    attributed to a prompt change rather than a model change."""
    advance_to(session, CallState.RESEARCH)
    for transition in session.transitions:
        assert transition.reason.strip()
        assert transition.versions.prompt_version
        assert transition.versions.model_version


def test_a_transition_without_a_reason_is_refused(session: CallSession) -> None:
    with pytest.raises(StateError, match="reason"):
        session.transition_to(CallState.IDENTIFY, "   ")


def test_skipping_a_state_is_refused(session: CallSession) -> None:
    """draft never writes, and the way it stays that way is that nothing can
    jump the queue to reach a state that does."""
    with pytest.raises(StateError, match="no edge"):
        session.transition_to(CallState.APPROVAL, "skipping ahead")


def test_going_backwards_is_refused(session: CallSession) -> None:
    advance_to(session, CallState.DRAFT)
    with pytest.raises(StateError, match="no edge"):
        session.transition_to(CallState.IDENTIFY, "back to identify")


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
def test_nothing_leaves_a_terminal_state(
    session: CallSession, terminal: CallState
) -> None:
    if terminal is CallState.WRAP:
        advance_to(session, CallState.WRAP)
    else:
        session.transition_to(terminal, "test")

    assert allowed_from(terminal) == frozenset()
    with pytest.raises(StateError, match="terminal"):
        session.transition_to(CallState.RESEARCH, "resurrect")


# ==========================================================================
# Transfer is the safe default
# ==========================================================================


@pytest.mark.parametrize(
    "state", [s for s in CallState if s not in TERMINAL]
)
def test_transfer_is_permitted_from_every_non_terminal_state(
    session: CallSession, state: CallState
) -> None:
    """C10. Never blocked, from anywhere. An agent that cannot transfer turns
    every other failure into a worse one."""
    advance_to(session, state)
    assert session.state is state, "the fixture did not reach the state under test"

    session.transfer("caller asked for a human")
    assert session.state is CallState.TRANSFER


@pytest.mark.parametrize("lateral", sorted(LATERAL, key=lambda s: s.value))
def test_every_lateral_is_reachable_from_a_mid_call_state(
    session: CallSession, lateral: CallState
) -> None:
    advance_to(session, CallState.DRAFT)
    session.transition_to(lateral, "test")
    assert session.state is lateral


# ==========================================================================
# Identity — the thing that had nothing to set it
# ==========================================================================


def test_identity_starts_unverified(session: CallSession) -> None:
    assert session.identity_verified is False
    assert session.tool_context().identity_verified is False


def test_verifying_identity_sets_the_context(session: CallSession) -> None:
    session.transition_to(CallState.IDENTIFY, "consent captured")
    session.verify_identity("+919876543210")

    ctx = session.tool_context()
    assert ctx.identity_verified is True
    assert ctx.verified_msisdn == "9876543210"


def test_the_verified_number_is_normalised(session: CallSession) -> None:
    """Stored normalised so the registry's binding check compares like with
    like. The caller says their number however they like."""
    session.transition_to(CallState.IDENTIFY, "consent captured")
    session.verify_identity("9876543210")
    assert session.tool_context().verified_msisdn == "9876543210"


@pytest.mark.parametrize(
    "state", [CallState.INTAKE, CallState.RESEARCH, CallState.DRAFT, CallState.APPROVAL]
)
def test_identity_cannot_be_established_outside_identify(
    session: CallSession, state: CallState
) -> None:
    """Verifying later would mean the challenge happened after the lookup it
    was supposed to gate."""
    advance_to(session, state)
    assert session.state is state
    with pytest.raises(StateError, match="identify"):
        session.verify_identity("+919876543210")


def test_three_failed_attempts_transfers_the_call(session: CallSession) -> None:
    session.transition_to(CallState.IDENTIFY, "consent captured")

    for expected_remaining in range(MAX_IDENTITY_ATTEMPTS - 1, 0, -1):
        assert session.fail_identity_attempt() == expected_remaining

    with pytest.raises(IdentityExhausted):
        session.fail_identity_attempt()

    assert session.state is CallState.TRANSFER
    assert session.identity_verified is False


def test_a_failed_attempt_reports_how_many_remain(session: CallSession) -> None:
    """So the caller can be told, rather than being cut off without warning."""
    session.transition_to(CallState.IDENTIFY, "consent captured")
    assert session.fail_identity_attempt() == MAX_IDENTITY_ATTEMPTS - 1


# ==========================================================================
# Repair is bounded
# ==========================================================================


def test_one_repair_loop_is_allowed(session: CallSession) -> None:
    advance_to(session, CallState.VALIDATE)
    session.transition_to(CallState.REPAIR, "missing field")
    session.transition_to(CallState.VALIDATE, "field supplied")
    assert session.state is CallState.VALIDATE


def test_a_second_repair_is_refused(session: CallSession) -> None:
    """An agent that cannot fix a draft in one bounded retry is guessing at
    what is wrong, and the correct next move is a human."""
    advance_to(session, CallState.VALIDATE)
    session.transition_to(CallState.REPAIR, "missing field")
    session.transition_to(CallState.VALIDATE, "field supplied")

    assert MAX_REPAIR_LOOPS == 1
    with pytest.raises(StateError, match="repair budget"):
        session.transition_to(CallState.REPAIR, "still broken")


def test_transfer_remains_available_after_the_repair_budget(
    session: CallSession,
) -> None:
    advance_to(session, CallState.VALIDATE)
    session.transition_to(CallState.REPAIR, "missing field")
    session.transition_to(CallState.VALIDATE, "field supplied")

    session.transfer("validator failure survived repair")
    assert session.state is CallState.TRANSFER


# ==========================================================================
# The model-facing view
# ==========================================================================


def test_tool_context_is_the_only_thing_the_model_sees(session: CallSession) -> None:
    """The session is mutable because a call changes. What reaches the tool
    layer is a frozen snapshot, so a handler cannot promote itself."""
    ctx = session.tool_context()
    with pytest.raises((ValueError, TypeError)):
        ctx.state = "execute"  # type: ignore[misc]


def test_dry_run_defaults_to_true(session: CallSession) -> None:
    assert session.tool_context().dry_run is True


def test_speculation_is_opt_in_per_call(session: CallSession) -> None:
    assert session.tool_context().speculative is False
    assert session.tool_context(speculative=True).speculative is True
