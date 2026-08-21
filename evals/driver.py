"""Drive one case through the real agent and record everything.

This is the text-level pipeline: fencing, the model, the registry, the state
machine, the clinical guard, the audit log. Speech is not in it. STT and TTS
sit on either end of exactly this loop -- `chat.py` says the same thing about
the same loop -- so what a baseline from here measures is reasoning, tool
choice and grounding, and what it does not measure is whether the agent heard
correctly.

That distinction has to survive into the results, not live in a docstring. One
case (`edge-006`) is built on acoustics and carries audio fixtures. It is
reported as NOT RUN, never as passed, because a case about degrading SNR
scored over clean text is a case that tests nothing and says it passed.

Nothing here decides pass or fail. It records; `score.py` judges. Keeping those
apart is what lets the scorer be re-run over a stored transcript without
spending another call.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from evals.faults import FaultingAdapter, wrap_if_needed
from evals.schema import EvalCase, Fault
from evals.world import World, build_world, seed_existing_booking, seeded_adapter
from voicedesk.agent import TurnTrace
from voicedesk.llm import LanguageModel
from voicedesk.state import CallState
from voicedesk.tools.registry import ToolRegistry
from voicedesk.tools.schemas import ToolContext
from voicedesk.tools.schemas import ToolResult as RegistryResult

TERMINAL_STATES = {"transfer", "wrap", "abandoned", "failed"}

NOT_RUNNABLE_AUDIO = "carries audio fixtures; this harness is text-level"


@dataclass
class ToolInvocation:
    """One tool attempt, with the state the call was in when it was made.

    The audit row does not carry the state, and an unauthorized write is
    defined by the state it happened from. Recorded here rather than by
    changing the audit table, because the harness needing a field is not a
    reason to widen a production schema.
    """

    name: str
    state: str
    ok: bool
    error_code: str | None
    args: dict[str, Any]
    payload: dict[str, Any]
    identity_verified: bool = False
    """Whether the caller had passed the DOB challenge at the moment of the
    call. An unverified mutation is defined by this, not by whether identity
    was ever established later in the call."""


@dataclass
class RunRecord:
    """Everything one call produced. No verdicts."""

    case_id: str
    runnable: bool = True
    not_runnable_reason: str | None = None

    traces: list[TurnTrace] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    latencies_ms: list[int] = field(default_factory=list)
    error: str | None = None

    world: World | None = None
    faults: FaultingAdapter | None = None
    caller_turns_delivered: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    throttled: int = 0
    """Times the provider rate-limited this case and the harness waited.

    Reported rather than swallowed: a run that needed forty retries produced
    its numbers under conditions worth knowing about, even though none of them
    are the agent's doing."""

    model_calls: int = 0
    """Model round-trips, not caller turns. One turn can be several: the agent
    loop runs up to MAX_TOOL_ROUNDS model->tools->model cycles per utterance,
    and a per-turn count would understate the spend by whatever that number
    happens to be."""

    @property
    def utterances(self) -> list[str]:
        return [t.spoken_text for t in self.traces if t.spoken_text]

    @property
    def ok_payloads(self) -> list[dict[str, Any]]:
        """Only what the agent was actually told. A refused call teaches it
        nothing it may then repeat as fact."""
        return [i.payload for i in self.invocations if i.ok]

    @property
    def tools_called(self) -> set[str]:
        return {i.name for i in self.invocations}

    @property
    def tools_succeeded(self) -> set[str]:
        return {i.name for i in self.invocations if i.ok}

    @property
    def turns_used(self) -> int:
        """All turns, caller and agent -- the scope max_total_turns means.

        `traces` holds the opening plus one entry per caller utterance handled,
        so a five-turn call has six traces and used eleven turns: the opening,
        then five caller/agent pairs.

        Written as `1 + 2 * len(traces)` first, which counts the opening twice
        and reports 13 for that call. `normal-001` allows 12, so a conversation
        two turns inside its budget would have failed on turn count -- a
        harness defect that reads in the results table exactly like an agent
        that rambles.
        """
        return max(1, 2 * len(self.traces) - 1)


THROTTLE_RETRIES = 3
THROTTLE_BACKOFF_S = 4.0
"""Doubling, so 4s / 8s / 16s. Long by the standards of a live call and
correct by the standards of a batch."""


class _RecordingModel:
    """Wraps the model to total up token spend, and to survive a throttle.

    A proxy for the same reason the registry gets one: the agent must talk to
    the model exactly as it does in production, and the harness must not be a
    second code path through the seam.

    **The retry lives here and not in `OpenRouterModel`, deliberately.** Running
    58 cases six at a time is a burst no live call produces, and the shakedown
    run lost two cases to a 429 -- which the scorer recorded as a crashed call
    and voided, indistinguishable at a glance from an agent that fell over. A
    provider throttle is the harness's problem, not the agent's, and it must not
    reach the baseline as either a pass or a failure.

    In a live call the correct response to a throttled provider is the opposite
    one: transfer. A caller does not wait sixteen seconds in silence. Two
    contexts, two correct behaviours, and putting the batch behaviour into the
    seam would give the caller the wrong one.
    """

    def __init__(self, inner: LanguageModel, record: RunRecord) -> None:
        self._inner = inner
        self._record = record

    async def respond(self, **kwargs: Any) -> Any:
        turn = await self._with_retry(kwargs)
        self._record.prompt_tokens += turn.prompt_tokens
        self._record.completion_tokens += turn.completion_tokens
        self._record.model_calls += 1
        return turn

    async def _with_retry(self, kwargs: dict[str, Any]) -> Any:
        delay = THROTTLE_BACKOFF_S
        for attempt in range(THROTTLE_RETRIES + 1):
            try:
                return await self._inner.respond(**kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised unless it is a throttle
                if attempt == THROTTLE_RETRIES or not _is_throttle(exc):
                    raise
                self._record.throttled += 1
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")  # pragma: no cover


def _is_throttle(exc: Exception) -> bool:
    """Matched on the message rather than the type.

    `ModelUnavailable` covers a retired model, a denied project and a revoked
    key as well, and retrying any of those three is a slower way to fail.
    """
    text = str(exc).lower()
    return "rate-limit" in text or "rate limit" in text or "429" in text


class _RecordingRegistry:
    """Wraps `ToolRegistry.invoke` to capture the call state and the payload.

    A proxy rather than a subclass: the registry is constructed by
    `register_scheduling_tools`, and swapping the type under it would mean the
    harness and production build their tool surface differently.
    """

    def __init__(self, inner: ToolRegistry, sink: list[ToolInvocation]) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def invoke(
        self, name: str, args: dict[str, Any], ctx: ToolContext
    ) -> RegistryResult:
        result = await self._inner.invoke(name, args, ctx)
        self._sink.append(
            ToolInvocation(
                name=name,
                state=ctx.state,
                ok=result.ok,
                error_code=result.error_code,
                args=dict(args),
                payload=dict(result.data or {}) if result.ok else {},
                identity_verified=ctx.identity_verified,
            )
        )
        return result


async def run_case(case: EvalCase, model: LanguageModel, *, model_version: str) -> RunRecord:
    """One case, one call, one record."""
    record = RunRecord(case_id=case.id)

    if any(turn.audio_fixture for turn in case.turns):
        record.runnable = False
        record.not_runnable_reason = NOT_RUNNABLE_AUDIO
        return record

    declared = frozenset(case.inject)
    adapter = wrap_if_needed(seeded_adapter(case.tenant), declared)
    if case.expect.outcome.value in {"rescheduled", "cancelled"}:
        # Nothing to move or cancel otherwise: the agent correctly reports no
        # appointment, the case scores as a failed reschedule, and the FIXTURE
        # is what failed. `existing_ref` exists to declare this per case and no
        # case uses it yet -- see PROJECT.md section 5a.
        seed_existing_booking(_inner(adapter))

    record.world = build_world(
        model=model,
        tenant_slug=case.tenant,
        language=case.language,
        trace_id=f"eval-{case.id}",
        model_version=model_version,
        adapter=adapter,  # type: ignore[arg-type]
    )
    if isinstance(adapter, FaultingAdapter):
        record.faults = adapter

    agent = record.world.agent
    agent.registry = _RecordingRegistry(agent.registry, record.invocations)  # type: ignore[assignment]
    agent.model = _RecordingModel(agent.model, record)  # type: ignore[assignment]

    opening = agent.open()
    record.traces.append(TurnTrace(caller_text="", spoken_text=opening))

    for turn in case.turns:
        if agent.session.state.value in TERMINAL_STATES:
            break
        if record.turns_used >= case.expect.max_total_turns:
            # Stop at the ceiling rather than run past it. Scoring still fails
            # the case on turns_used; continuing would spend calls proving a
            # point already proved.
            break
        if not turn.say:  # pragma: no cover - schema requires say or audio
            continue

        started = time.perf_counter()
        try:
            trace = await agent.turn(turn.say)
        except Exception as exc:  # noqa: BLE001 - a crashed call is a result, not a stack trace
            record.error = f"{type(exc).__name__}: {exc}"
            break
        record.latencies_ms.append(int((time.perf_counter() - started) * 1000))
        record.traces.append(trace)
        record.caller_turns_delivered += 1

        if turn.then_hangup:
            break

    _release_on_hangup(record)
    return record


def _release_on_hangup(record: RunRecord) -> None:
    """A dropped call must not park a slot.

    STATE_MACHINE rule 4 and `adapters.base.release_hold`. A leaked hold removes
    a real slot from a real clinic's day and nothing in the transcript shows it,
    so the harness ends every call the way the pipeline must.
    """
    world = record.world
    if world is None:
        return
    if world.session.state in {CallState.WRAP}:
        return
    adapter = world.adapter
    held = [s for s in adapter.slots.values() if s.held_by_call == world.session.call_id]
    for slot in held:
        slot.held_until = None
        slot.held_by_call = None


def undeliverable_faults(case: EvalCase) -> frozenset[Fault]:
    """Faults this case declares that the harness cannot produce.

    Empty today. If it ever is not, the run is void rather than scored -- a
    case whose premise the harness cannot stage did not test what it exists to
    test, and reporting that is worth more than either verdict.
    """
    from evals.faults import UNIMPLEMENTED

    return frozenset(case.inject) & UNIMPLEMENTED


def _inner(adapter: Any) -> Any:
    return getattr(adapter, "inner", adapter)
