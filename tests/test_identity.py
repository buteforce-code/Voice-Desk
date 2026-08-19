"""Identity is established server-side, never asserted by the model.

The design this replaces put `identity_verified: Literal[True]` on three tool
INPUT schemas. The model fills tool arguments, and `Literal[True]` admits
exactly one value -- so the model always set it, validation always passed, and
nothing anywhere checked whether a challenge had actually happened. A control
that cannot fail is not a control.

The eval set had already noticed, and disagreed with itself about it:

  * `bad_input-009` -- "Literal[True] means 'I forgot to verify' is a schema
    error rather than a code path, but it does nothing whatsoever to stop a
    model from writing True because the field demanded it."
  * `malicious-003` -- "an unverified cancellation is not expressible in the
    schema; the tool call cannot be constructed."

The first was right. This module is the reason the second is now true too.

`find_appointments` was the sharp edge: AUTONOMOUS tier, so no approval token
and no required state, and the model supplied the phone number. It answered
"does this number have appointments at this clinic" for any number, at any
point in a call, for anyone who got the agent to ask.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import OTHER_MSISDN, VERIFIED_MSISDN, make_ctx, verified_ctx

from voicedesk.tools.schemas import (
    CancelIn,
    FindAppointmentsIn,
    RescheduleIn,
    ToolContext,
    normalize_msisdn,
)

IDENTITY_GATED = ["find_appointments", "reschedule_appointment", "cancel_appointment"]

ADAPTER_SRC = (
    Path(__file__).parent.parent / "src" / "voicedesk" / "adapters" / "postgres.py"
)


# ==========================================================================
# The model cannot assert its own verification
# ==========================================================================


@pytest.mark.parametrize("model", [FindAppointmentsIn, RescheduleIn, CancelIn])
def test_no_tool_input_schema_accepts_an_identity_claim(model: type) -> None:
    """The field is gone from the model-facing surface entirely. Not narrowed,
    not validated -- absent. A model cannot lie about something it is never
    asked."""
    assert "identity_verified" not in model.model_fields, (
        f"{model.__name__} still lets the model assert its own verification"
    )


def test_identity_lives_on_the_server_side_context() -> None:
    assert "identity_verified" in ToolContext.model_fields
    assert "verified_msisdn" in ToolContext.model_fields


def test_identity_defaults_to_unverified() -> None:
    """A context built without saying anything about identity is unverified.
    The direction of the default matters: True would mean every code path that
    forgets to set it is a breach."""
    ctx = make_ctx()
    assert ctx.identity_verified is False
    assert ctx.verified_msisdn is None


def test_context_is_frozen_so_a_handler_cannot_self_verify() -> None:
    """Tool handlers receive the context. If they could write to it, the gate
    would only be as strong as every handler anyone ever adds."""
    ctx = make_ctx()
    with pytest.raises((ValueError, TypeError)):
        ctx.identity_verified = True  # type: ignore[misc]


# ==========================================================================
# The gate refuses, and the refusal is audited
# ==========================================================================


@pytest.mark.parametrize("tool", IDENTITY_GATED)
async def test_identity_gated_tool_is_refused_without_verification(
    registry, audit, tool: str
) -> None:
    ctx = make_ctx(state="approval", approval_token="tok")  # noqa: S106 - test fixture
    result = await registry.invoke(tool, {}, ctx)

    assert result.ok is False
    assert result.error_code == "identity_not_verified"
    assert audit.results_for(tool) == ["rejected"]


async def test_find_appointments_is_refused_even_though_it_is_autonomous(
    registry,
) -> None:
    """The one that mattered. AUTONOMOUS means no approval token and no state
    requirement, so had the identity check been written inside the
    explicit-approval branch, this tool would have skipped it entirely."""
    result = await registry.invoke("find_appointments", {}, make_ctx())

    assert result.ok is False
    assert result.error_code == "identity_not_verified"


async def test_find_appointments_succeeds_once_identity_is_established(
    registry, adapter
) -> None:
    result = await registry.invoke("find_appointments", {}, verified_ctx())

    assert result.ok is True
    assert "find_appointments" in adapter.calls


# ==========================================================================
# The subject comes from the context, not from the arguments
# ==========================================================================


def test_find_appointments_takes_no_phone_number_at_all() -> None:
    """Enumeration is not blocked, it is unsayable. There is no field in which
    to name somebody else."""
    assert "patient_msisdn" not in FindAppointmentsIn.model_fields


async def test_naming_another_number_is_a_schema_error_not_a_lookup(
    registry, adapter
) -> None:
    """`extra="forbid"` makes the attempt a validation failure rather than a
    silently dropped argument. Silently dropping would be worse: the agent
    would believe it had looked somebody else up, and narrate accordingly."""
    result = await registry.invoke(
        "find_appointments", {"patient_msisdn": OTHER_MSISDN}, verified_ctx()
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert "find_appointments" not in adapter.calls


async def test_lookup_uses_the_verified_number(registry, adapter, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def spy(clinic_id, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return []

    monkeypatch.setattr(adapter, "find_appointments", spy)
    await registry.invoke("find_appointments", {}, verified_ctx())

    assert seen["patient_msisdn"] == VERIFIED_MSISDN


# ==========================================================================
# Writes are bound to the verified caller in the adapter, not only the registry
# ==========================================================================


@pytest.mark.parametrize("method", ["reschedule", "cancel"])
def test_write_adapter_calls_are_scoped_by_the_verified_number(method: str) -> None:
    """The registry gate says "somebody verified". This says "and the row you
    are about to change belongs to them" -- enforced by a join in the SQL, so a
    guessed or overheard appointment_id matches no row rather than matching and
    then being refused. Both failures look identical to the caller, which is
    the point.
    """
    src = ADAPTER_SRC.read_text(encoding="utf-8")
    body = src.split(f"async def {method}(")[1].split("    async def ")[0]

    assert "patient_msisdn" in body, f"{method}() does not take the verified number"
    assert "p.msisdn = $" in body, (
        f"{method}() does not filter on the verified number in SQL"
    )


@pytest.mark.parametrize("tool", ["reschedule_appointment", "cancel_appointment"])
def test_write_tools_declare_that_they_require_identity(registry, tool: str) -> None:
    assert registry._tools[tool].requires_identity is True


def test_confirm_booking_does_not_require_prior_identity(registry) -> None:
    """Deliberate asymmetry, stated so it is not read as an oversight.

    A first-time caller booking their own appointment has no record to be
    verified against, so requiring verification would make the primary happy
    path impossible. Reading or changing an EXISTING booking is the act that
    needs a verified subject, because that is where another patient's data
    becomes reachable.
    """
    assert registry._tools["confirm_booking"].requires_identity is False


# ==========================================================================
# Number normalisation -- the dull bug that would break the gate in practice
# ==========================================================================


@pytest.mark.parametrize(
    "written", ["+919876543210", "919876543210", "9876543210"]
)
def test_the_same_subscriber_normalises_identically(written: str) -> None:
    """The Msisdn pattern accepts all three spellings. A binding check that
    compared raw strings would refuse the right caller for saying their own
    number differently than the record stores it -- and a security control that
    rejects legitimate callers gets relaxed, not debugged."""
    assert normalize_msisdn(written) == "9876543210"


def test_different_subscribers_do_not_normalise_together() -> None:
    assert normalize_msisdn(VERIFIED_MSISDN) != normalize_msisdn(OTHER_MSISDN)
