"""Tool registry — the enforcement point for G4.

Everything the standard demands of a tool happens here, once, for every tool:
strict schema validation, **server-side** authorization, idempotency, rate
limits, dry-run, and an audit row per attempt.

The rule this file exists to enforce:

    A model choosing to call a tool is not authorization.

Nothing below reads authority from model output.  Tier, state and approval
token all come from the call session, which the model cannot write to.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ValidationError

from voicedesk.tools.schemas import Tier, ToolContext, ToolResult, normalize_msisdn

log = structlog.get_logger(__name__)

MAX_TOOL_CALLS_PER_CALL = 25
MAX_CALLS_PER_TOOL_PER_CALL = 8


class ToolError(Exception):
    """Base for refusals. Never leaks internals to the caller-facing layer."""

    code = "tool_error"


class SpeculationNotPermitted(ToolError):
    code = "speculation_not_permitted"


class NotAuthorized(ToolError):
    code = "not_authorized"


class IdentityNotVerified(ToolError):
    """The call never completed a DOB challenge, so there is no verified
    subject to act on behalf of."""

    code = "identity_not_verified"


class IdentityMismatch(ToolError):
    """The arguments name a different patient than the one who verified.
    Should be unreachable -- no identity-gated tool accepts an msisdn -- but a
    boundary that depends on a schema staying a certain shape is worth
    asserting rather than assuming."""

    code = "identity_mismatch"


class RateLimited(ToolError):
    code = "rate_limited"


class AuditSink(Protocol):
    """Append-only. Writes one `agent_actions` row per attempt."""

    async def record(
        self,
        ctx: ToolContext,
        tool_name: str,
        args_redacted: dict[str, Any],
        result: str,
        idempotency_key: str,
        authorized_by: str,
        rejection_reason: str | None = None,
    ) -> None: ...

    async def find_replay(self, idempotency_key: str) -> dict[str, Any] | None: ...


Handler = Callable[[BaseModel, ToolContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: Tier
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler
    side_effect_free: bool
    requires_idempotency: bool
    requires_identity: bool

    @property
    def speculatable(self) -> bool:
        """Speculative prefetch is safe only when a tool both sits in the
        autonomous tier AND mutates nothing.

        `hold_slot` is autonomous but mutating, so it is excluded — speculating
        on a mutation means acting on a half-heard sentence, which is the
        excessive-agency failure the risk register exists to prevent.
        """
        return self.tier is Tier.AUTONOMOUS and self.side_effect_free


class ToolRegistry:
    def __init__(self, audit: AuditSink) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._audit = audit
        self._counts: dict[tuple[str, str], int] = {}

    # -- registration ----------------------------------------------------

    def register(
        self,
        name: str,
        *,
        tier: Tier,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        side_effect_free: bool,
        requires_identity: bool = False,
    ) -> Callable[[Handler], Handler]:
        if tier is Tier.PROHIBITED:
            # Prohibited capabilities are enforced by not existing. If this
            # ever fires, someone tried to implement one.
            raise ValueError(f"{name}: prohibited capabilities have no implementation")

        def decorate(fn: Handler) -> Handler:
            self._tools[name] = ToolSpec(
                name=name,
                tier=tier,
                input_model=input_model,
                output_model=output_model,
                handler=fn,
                side_effect_free=side_effect_free,
                requires_idempotency=not side_effect_free,
                requires_identity=requires_identity,
            )
            return fn

        return decorate

    def schema_for_llm(self) -> list[dict[str, Any]]:
        """Function declarations handed to the model. Deliberately excludes
        tier, so the model is never told which tools are 'more powerful'."""
        return [
            {
                "name": spec.name,
                "parameters": spec.input_model.model_json_schema(),
            }
            for spec in self._tools.values()
        ]

    # -- invocation ------------------------------------------------------

    async def invoke(
        self, name: str, raw_args: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        started = time.perf_counter()
        spec = self._tools.get(name)

        # An unregistered name is a model hallucinating a capability, or
        # probing for one. Both are worth an audit row.
        if spec is None:
            await self._audit.record(
                ctx, name, {}, "rejected", _key(ctx, name),
                "none", f"unknown tool: {name}",
            )
            log.warning("tool.unknown", tool=name, trace_id=ctx.trace_id)
            return ToolResult(
                ok=False, error_code="unknown_tool",
                error_message="No such tool.",
            )

        try:
            self._check_rate_limit(ctx, spec)
            self._check_speculation(ctx, spec)
            # Authorize BEFORE validating. `_authorize` reads only ctx and spec,
            # never the arguments, so the order is free — and this way an
            # unauthorized attempt is audited as 'not_authorized' rather than as
            # whatever its malformed arguments happened to trip first. The audit
            # log exists to show attempted unauthorized writes; a row reading
            # "schema validation failed" loses exactly that signal. It also
            # denies an unauthorized caller a validation-error oracle.
            authorized_by = self._authorize(ctx, spec)
            args = self._validate(spec, raw_args)
            self._check_identity_binding(args, ctx, spec)
        except ToolError as exc:
            await self._audit.record(
                ctx, name, _redact(raw_args), "rejected",
                _key(ctx, name), "none", str(exc),
            )
            log.warning("tool.rejected", tool=name, code=exc.code, trace_id=ctx.trace_id)
            return ToolResult(ok=False, error_code=exc.code, error_message=str(exc))
        except ValidationError as exc:
            await self._audit.record(
                ctx, name, _redact(raw_args), "rejected",
                _key(ctx, name), "none", "schema validation failed",
            )
            log.warning("tool.invalid", tool=name, errors=exc.error_count())
            return ToolResult(
                ok=False, error_code="invalid_arguments",
                error_message="Arguments did not match the tool schema.",
            )

        idem = _idempotency_key(args, ctx, spec)

        if spec.requires_idempotency:
            replay = await self._audit.find_replay(idem)
            if replay is not None:
                log.info("tool.replay", tool=name, trace_id=ctx.trace_id)
                return ToolResult(ok=True, data=replay, idempotent_replay=True)

        if ctx.dry_run and not spec.side_effect_free:
            await self._audit.record(
                ctx, name, _redact(raw_args), "success", idem, f"{authorized_by}:dry_run",
            )
            log.info("tool.dry_run", tool=name, trace_id=ctx.trace_id)
            return ToolResult(ok=True, data={"dry_run": True}, error_code=None)

        try:
            out = await spec.handler(args, ctx)
        except Exception as exc:  # noqa: BLE001 - boundary: nothing escapes to the caller
            await self._audit.record(
                ctx, name, _redact(raw_args), "error", idem, authorized_by, repr(exc),
            )
            log.error("tool.error", tool=name, trace_id=ctx.trace_id, exc_info=True)
            return ToolResult(
                ok=False, error_code="tool_failed",
                error_message="The action could not be completed.",
            )

        await self._audit.record(
            ctx, name, _redact(raw_args), "success", idem, authorized_by,
        )
        log.info(
            "tool.ok", tool=name, trace_id=ctx.trace_id,
            ms=round((time.perf_counter() - started) * 1000),
        )
        return ToolResult(ok=True, data=out.model_dump(mode="json"))

    # -- guards ----------------------------------------------------------

    def _validate(self, spec: ToolSpec, raw: dict[str, Any]) -> BaseModel:
        return spec.input_model.model_validate(raw)

    def _authorize(self, ctx: ToolContext, spec: ToolSpec) -> str:
        """Server-side authorization. Derived from session state, never from
        anything the model produced."""
        # Identity is checked before tier, and for every tier. find_appointments
        # is AUTONOMOUS -- it needs no approval token and no particular state --
        # so if this check lived inside the EXPLICIT_APPROVAL branch the one
        # tool that actually leaks health data would skip it entirely.
        if spec.requires_identity and not ctx.identity_verified:
            raise IdentityNotVerified(
                f"{spec.name} requires a verified caller; the identify state "
                f"has not completed a successful challenge"
            )

        if spec.tier is Tier.AUTONOMOUS:
            return "autonomous"

        if spec.tier is Tier.EXPLICIT_APPROVAL:
            # A write happens in `execute`, and `execute` has exactly one
            # inbound edge, from `approval` -- asserted at import time in
            # state.py, not merely reviewed.
            #
            # This previously authorized on state == "approval". That reading
            # made the invariant docs/STATE_MACHINE.md calls "the single most
            # important in the system" decorative: if the write already
            # happened during approval, it no longer matters what `execute` is
            # reachable from. Requiring `execute` makes BOTH the state graph
            # and the token load-bearing -- the token proves the caller
            # confirmed, and the graph proves the confirmation came first.
            # See PROJECT.md D13.
            if ctx.state != "execute":
                raise NotAuthorized(
                    f"{spec.name} requires state 'execute', got '{ctx.state}'"
                )
            if not ctx.approval_token:
                raise NotAuthorized(
                    f"{spec.name} requires an approval token minted at approval"
                )
            return "caller_confirmation"

        raise NotAuthorized(f"{spec.name}: tier {spec.tier} has no approval path")

    def _check_identity_binding(
        self, args: BaseModel, ctx: ToolContext, spec: ToolSpec
    ) -> None:
        """An identity-gated tool may not name a subject other than the caller.

        No current tool accepts an msisdn, so this never fires today. It exists
        because the previous design's whole failure was that the subject came
        from model output: if someone reintroduces such a field, it is bound to
        the verified caller automatically rather than trusted.
        """
        if not spec.requires_identity:
            return

        supplied = getattr(args, "patient_msisdn", None)
        if supplied is None:
            return

        if ctx.verified_msisdn is None or (
            normalize_msisdn(supplied) != normalize_msisdn(ctx.verified_msisdn)
        ):
            raise IdentityMismatch(
                f"{spec.name} was called for a number other than the verified caller"
            )

    def _check_speculation(self, ctx: ToolContext, spec: ToolSpec) -> None:
        if ctx.speculative and not spec.speculatable:
            raise SpeculationNotPermitted(
                f"{spec.name} may not be invoked speculatively"
            )

    def _check_rate_limit(self, ctx: ToolContext, spec: ToolSpec) -> None:
        total = sum(n for (call, _), n in self._counts.items() if call == ctx.trace_id)
        if total >= MAX_TOOL_CALLS_PER_CALL:
            raise RateLimited("tool call budget exhausted for this call")

        k = (ctx.trace_id, spec.name)
        if self._counts.get(k, 0) >= MAX_CALLS_PER_TOOL_PER_CALL:
            raise RateLimited(f"{spec.name} called too many times in one call")
        self._counts[k] = self._counts.get(k, 0) + 1


# --------------------------------------------------------------------------


def _key(ctx: ToolContext, name: str) -> str:
    return f"{ctx.trace_id}:{name}:rejected:{time.time_ns()}"


def _idempotency_key(args: BaseModel, ctx: ToolContext, spec: ToolSpec) -> str:
    """Prefer the caller-supplied key; fall back to a deterministic one so a
    retried read never double-counts."""
    supplied = getattr(args, "idempotency_key", None)
    if supplied:
        return f"{ctx.clinic_id}:{supplied}"
    return f"{ctx.trace_id}:{spec.name}"


_SENSITIVE = {"patient_msisdn", "patient_display_name", "dob", "context_summary"}


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Audit rows are queried by staff. They do not need the PII."""
    return {
        k: ("<redacted>" if k in _SENSITIVE else v)
        for k, v in args.items()
    }
