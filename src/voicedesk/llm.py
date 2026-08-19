"""The model seam.

Everything above this file is written against `LanguageModel`, never against
Gemini. Three reasons, and the third is the one that matters most:

  * G7 requires a provider fallback. A seam is what makes that a config change.
  * The whole agent loop stays testable without an API key, because
    `ScriptedModel` satisfies the same Protocol.
  * **Automatic function calling is disabled.** The Gemini SDK will happily
    execute tool calls itself, and that would route every write around
    `ToolRegistry` -- past the tier check, the approval token, the identity
    gate and the audit row. The single most important line in this module is
    `AutomaticFunctionCallingConfig(disable=True)`.

The model returns *intent*: some text, and zero or more tool calls it would
like made. Whether any of them actually happen is decided elsewhere, by code
the model cannot reach.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass(frozen=True)
class ToolCall:
    """A request. Not a permission, and not a guarantee it will run."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: str
    """'caller' | 'agent' | 'tool'"""
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LanguageModel(Protocol):
    async def respond(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelTurn: ...


# --------------------------------------------------------------------------
# Gemini, via Google AI Studio
# --------------------------------------------------------------------------


class GeminiModel:
    """Google AI Studio only.

    Vertex AI is a standing org-wide block (`config.py` refuses to start with
    it enabled), so this constructs the client with an explicit API key and
    never falls back to application-default credentials.
    """

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def respond(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelTurn:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            # Booking is not a creative task. Low temperature is a latency and
            # a correctness decision, not a style one.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            # ^ Do not remove. With automatic calling on, the SDK executes tools
            # itself and every write skips ToolRegistry -- the tier check, the
            # approval token, the identity gate and the audit row. The model
            # would be authorizing itself, which is the exact failure hard rule
            # 6 names.
            tools=[types.Tool(function_declarations=list(tools))] if tools else None,
        )

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_to_gemini(history),
            config=config,
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else []) or []:
                if getattr(part, "text", None):
                    text_parts.append(part.text)
                fn = getattr(part, "function_call", None)
                if fn is not None:
                    calls.append(ToolCall(name=fn.name, args=dict(fn.args or {})))

        return ModelTurn(text=" ".join(text_parts).strip(), tool_calls=tuple(calls))


def _to_gemini(history: Sequence[Message]) -> list[dict[str, Any]]:
    """Flatten our history into Gemini `contents`.

    Tool results are rendered as text rather than as `function_response` parts.
    That is deliberate: it keeps one representation of a turn across every
    provider behind this seam, and it means a provider swap does not silently
    change what the model sees.
    """
    contents: list[dict[str, Any]] = []
    for message in history:
        if message.role == "caller":
            contents.append({"role": "user", "parts": [{"text": message.text}]})
        elif message.role == "agent" and message.text:
            contents.append({"role": "model", "parts": [{"text": message.text}]})
        elif message.role == "tool":
            rendered = "\n".join(
                f"{r.name} returned: {json.dumps(r.payload, default=str)}"
                for r in message.tool_results
            )
            fenced = f"<tool_results>\n{rendered}\n</tool_results>"
            contents.append({"role": "user", "parts": [{"text": fenced}]})
    return contents


# --------------------------------------------------------------------------
# Scripted, for tests and for running without a key
# --------------------------------------------------------------------------


@dataclass
class ScriptedModel:
    """Replays a fixed list of turns.

    Not a mock in the usual sense -- it satisfies the same Protocol Gemini
    does, so the agent loop under test is the identical code path. What it
    removes is the model's judgement, which is the part that cannot be asserted
    deterministically anyway.
    """

    turns: list[ModelTurn]
    seen: list[Sequence[Message]] = field(default_factory=list)
    system_prompts: list[str] = field(default_factory=list)
    _index: int = 0

    async def respond(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelTurn:
        self.seen.append(list(history))
        self.system_prompts.append(system)

        if self._index >= len(self.turns):
            # Running off the end means the loop asked for more turns than the
            # scenario provides. Silence would look like the agent finishing.
            raise AssertionError(
                f"ScriptedModel exhausted after {len(self.turns)} turns -- the "
                f"agent asked for another one"
            )

        turn = self.turns[self._index]
        self._index += 1
        return turn


def build_model(
    api_key: str | None, *, model: str = DEFAULT_MODEL
) -> LanguageModel | None:
    """Returns None when no key is configured, so callers can degrade openly.

    Deliberately not a silent stub: a pipeline that quietly runs on a fake
    model would produce an eval baseline that means nothing.
    """
    if not api_key:
        return None
    return GeminiModel(api_key, model=model)
