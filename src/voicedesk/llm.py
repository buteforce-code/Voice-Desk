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

# PROJECT.md's stack table named gemini-2.5-flash. As of 2026-08-20 the API
# refuses it for new keys:
#
#     404 NOT_FOUND — This model models/gemini-2.5-flash is no longer available
#     to new users. Please update your code to use models/gemini-3.6-flash
#
# Overridable via GEMINI_MODEL, because this will happen again. A model name is
# the shortest-lived constant in the file.
DEFAULT_MODEL = "gemini-3.6-flash"


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
            tools=(
                [types.Tool(function_declarations=to_function_declarations(tools))]
                if tools
                else None
            ),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=_to_gemini(history),
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below, or wrapped
            raise _friendlier(exc, self._model) from exc

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


# Gemini's FunctionDeclaration takes a SUBSET of OpenAPI 3.0, not arbitrary
# JSON Schema. Pydantic emits the full thing, and the live API rejects the
# difference outright:
#
#     400 INVALID_ARGUMENT — Unknown name "additional_properties"
#
# `extra="forbid"` is what produces `additionalProperties: false`, so the very
# setting that makes the tool schemas strict is the one Gemini refuses. Nothing
# below the model seam could have caught this: ScriptedModel accepts any dict,
# and the schemas are valid JSON Schema. It took one call to the real API.
#
# Translation lives here rather than in the registry on purpose. The registry
# stays provider-neutral -- this is exactly the kind of per-provider quirk the
# seam exists to absorb.
_GEMINI_KEYS = frozenset(
    {"type", "description", "enum", "items", "properties", "required", "nullable"}
)
_GEMINI_FORMATS = frozenset({"date-time", "enum"})


def _gemini_schema(node: Any, defs: dict[str, Any] | None = None) -> Any:
    """Reduce a Pydantic JSON Schema to what Gemini accepts."""
    if not isinstance(node, dict):
        return node

    defs = defs or node.get("$defs") or {}

    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _gemini_schema(defs.get(name, {}), defs)

    # Optional[X] becomes anyOf[X, null]. Gemini expresses the same thing as
    # the bare type plus nullable, and rejects anyOf here.
    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        nullable = len(variants) != len(node["anyOf"])
        collapsed = _gemini_schema(variants[0], defs) if variants else {"type": "string"}
        if nullable and isinstance(collapsed, dict):
            collapsed["nullable"] = True
        return collapsed

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "format":
            # 'uuid', 'email' and friends are not in Gemini's format enum.
            if value in _GEMINI_FORMATS:
                out[key] = value
            continue
        if key not in _GEMINI_KEYS:
            # Drops additionalProperties, title, default, pattern, minimum,
            # maximum, minLength, maxLength. Losing the constraints costs
            # nothing: Pydantic re-validates every argument on the way in, so
            # the schema is a hint to the model and the registry is the check.
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            out[key] = _gemini_schema(value, defs)
        else:
            out[key] = value

    out.setdefault("type", "object" if "properties" in out else "string")
    return out


def to_function_declarations(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registry tool specs -> Gemini function declarations."""
    declarations = []
    for tool in tools:
        declaration: dict[str, Any] = {"name": tool["name"]}
        if tool.get("description"):
            declaration["description"] = tool["description"]
        declaration["parameters"] = _gemini_schema(tool.get("parameters", {}))
        declarations.append(declaration)
    return declarations


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


class ModelUnavailable(RuntimeError):
    """The configured model name is not usable with this key."""


def _friendlier(exc: Exception, model: str) -> Exception:
    """Turn a provider 404 into something that says what to change.

    Model names get retired and a raw traceback about `models/x` does not tell
    anyone that GEMINI_MODEL is the knob. This cost a debugging round the first
    time it happened; it should not cost a second.
    """
    text = str(exc)

    if "PERMISSION_DENIED" in text or "denied access" in text:
        return ModelUnavailable(
            "Google AI Studio refused this API key: the project has been denied "
            "access. That is an account issue, not a configuration one -- check "
            "the project in aistudio.google.com (region availability, terms "
            "acceptance, billing), or issue a key from a different project. "
            "Nothing in .env will fix it."
        )

    if "NOT_FOUND" in text or "not available" in text or "no longer available" in text:
        suggestion = ""
        if "use models/" in text:
            suggestion = text.split("use models/")[-1].split()[0].strip(" .'\"")
        hint = f" Try GEMINI_MODEL={suggestion}" if suggestion else ""
        return ModelUnavailable(
            f"The model {model!r} is not available to this API key.{hint} "
            f"Set GEMINI_MODEL in .env to a model your key can use."
        )
    return exc
