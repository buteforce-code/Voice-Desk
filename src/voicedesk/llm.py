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
    call_id: str = ""
    """The provider's correlation id, echoed back so a result can be paired to
    the call that asked for it.

    OpenAI-compatible APIs require it and reject a `tool` message without a
    matching `tool_call_id`. Gemini pairs by name instead and supplies none, so
    this is empty on that path -- which is why nothing below the seam may read
    it for anything but pairing."""


@dataclass(frozen=True)
class ToolResult:
    name: str
    payload: dict[str, Any]
    call_id: str = ""


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

    prompt_tokens: int = 0
    completion_tokens: int = 0
    """What the provider says this turn cost, in tokens.

    Tokens, not rupees. G7 owns the cost ledger and the price table that turns
    one into the other; recording a currency figure here would mean inventing a
    rate for whichever provider happened to serve the call, and a fabricated
    cost is the same sin as a fabricated booking wearing a finance hat.

    Zero means the provider reported nothing, not that the turn was free.
    """

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
                    calls.append(
                        ToolCall(
                            name=fn.name,
                            args=dict(fn.args or {}),
                            call_id=f"gemini-{len(calls)}",
                        )
                    )

        usage = getattr(response, "usage_metadata", None)
        return ModelTurn(
            text=" ".join(text_parts).strip(),
            tool_calls=tuple(calls),
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            completion_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


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

    Tool calls and results are `function_call` / `function_response` parts --
    the provider-native shape, not text.

    They were text until the eval suite showed what that costs. Rendering a
    call as `<tool_calls>find_slots({...})</tool_calls>` puts the calling
    convention *in the content channel*, and a model reading its own history
    cannot tell a transcript of what it did from an example of how to speak.
    It stopped calling `find_slots` and started saying the tag out loud, in
    Tamil, to the caller. The native parts are a different field on the wire,
    so the confusion is not expressible.

    The earlier docstring here defended text on the grounds that one
    representation across providers keeps a swap from changing what the model
    sees. That is true and it is not worth this: what a provider swap must
    preserve is the *meaning* of a turn, and every provider behind this seam
    has a native encoding for exactly this meaning. Uniform-but-wrong is not
    the invariant worth holding.
    """
    contents: list[dict[str, Any]] = []
    for message in history:
        if message.role == "caller":
            contents.append({"role": "user", "parts": [{"text": message.text}]})
        elif message.role == "agent" and message.tool_calls:
            contents.append({
                "role": "model",
                "parts": [
                    {"function_call": {"name": c.name, "args": c.args}}
                    for c in message.tool_calls
                ],
            })
        elif message.role == "agent" and message.text:
            contents.append({"role": "model", "parts": [{"text": message.text}]})
        elif message.role == "tool":
            # Gemini pairs a response to its call by NAME, not by id, and takes
            # them as user-role parts. `response` must be an object: a bare
            # list or scalar is rejected.
            contents.append({
                "role": "user",
                "parts": [
                    {"function_response": {"name": r.name, "response": r.payload}}
                    for r in message.tool_results
                ],
            })
    return contents


# --------------------------------------------------------------------------
# OpenRouter — one key, many providers, OpenAI-compatible
# --------------------------------------------------------------------------


class OpenRouterModel:
    """OpenAI-compatible chat completions via OpenRouter.

    Added because Google AI Studio refused the project with a 403 that no
    configuration could fix. This is the seam earning its keep: the agent loop,
    the registry, the state machine and every test above it are untouched.

    **A data-protection note, recorded rather than buried.** OpenRouter is an
    additional processor in the chain -- caller utterances transit their
    infrastructure on the way to whichever provider serves the model. For the
    current stage that is fine: the tenant is fictional, no real patient exists,
    and D1a already accepted inference outside India (DPDP §16 permits it, and
    the residency obligation binds recordings at rest, which stay in Mumbai).

    Before any real patient call this needs a decision, not an assumption. See
    PROJECT.md D16.
    """

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "deepseek/deepseek-chat"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model or self.DEFAULT_MODEL
        self._url = base_url or self.BASE_URL
        self._timeout = timeout

    async def respond(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelTurn:
        import httpx

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai(system, history),
            "temperature": 0.2,
            # A caller turn is one or two sentences. Capping the reply is not
            # only cheaper -- a shorter completion reaches its last token
            # sooner, so the first audio starts sooner, and a reply that ends
            # before the caller's patience does is less likely to be talked
            # over. The prompt asks for brevity; this is what happens when the
            # model does not oblige.
            "max_tokens": 300,
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)
            # "auto", never "required": the agent must be free to answer without
            # calling anything, or every turn becomes a tool call and the caller
            # gets silence while it thrashes.
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Title": "Voice Desk",
                },
                json=payload,
            )

        if response.status_code != 200:
            raise _friendlier_openrouter(response.status_code, response.text, self._model)

        body = response.json()
        if "choices" not in body:
            raise ModelUnavailable(f"OpenRouter returned no choices: {str(body)[:200]}")

        message = body["choices"][0].get("message") or {}
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                # A model that emits unparseable arguments has hallucinated the
                # call. Pass an empty dict so the registry refuses it on schema
                # validation and the refusal is audited, rather than crashing
                # the turn.
                args = {}
            calls.append(
                ToolCall(
                    name=fn.get("name", ""),
                    args=args,
                    call_id=str(call.get("id") or f"call-{len(calls)}"),
                )
            )

        usage = body.get("usage") or {}
        return ModelTurn(
            text=(message.get("content") or "").strip(),
            tool_calls=tuple(calls),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


def to_openai_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registry specs -> OpenAI `tools`.

    No schema surgery needed here: the OpenAI shape accepts full JSON Schema,
    including the `additionalProperties` that Gemini rejects outright. That
    asymmetry is precisely why the translation lives per-provider.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
        }
        for tool in tools
    ]


def _to_openai(system: str, history: Sequence[Message]) -> list[dict[str, Any]]:
    """Flatten our history into OpenAI-compatible `messages`.

    Native `tool_calls` on the assistant turn, one `tool` message per result,
    paired by `tool_call_id`. See `_to_gemini` for why these are not text.

    The pairing is not cosmetic: an OpenAI-compatible endpoint rejects a `tool`
    message whose id matches no preceding assistant call, and silently ignores
    an assistant call that never gets a result.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in history:
        if message.role == "caller":
            messages.append({"role": "user", "content": message.text})
        elif message.role == "agent" and message.tool_calls:
            messages.append({
                "role": "assistant",
                "content": message.text or None,
                "tool_calls": [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.args, default=str),
                        },
                    }
                    for c in message.tool_calls
                ],
            })
        elif message.role == "agent" and message.text:
            messages.append({"role": "assistant", "content": message.text})
        elif message.role == "tool":
            for r in message.tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": r.call_id,
                    "content": json.dumps(r.payload, default=str),
                })
    return messages


def _friendlier_openrouter(status: int, body: str, model: str) -> Exception:
    if status in (401, 403):
        return ModelUnavailable(
            "OpenRouter rejected the API key. Check OPENROUTER_API_KEY in .env, "
            "and that the key has not been revoked."
        )
    if status == 402:
        return ModelUnavailable(
            f"OpenRouter reports insufficient credit for {model!r}. Pick a "
            f"free model -- set OPENROUTER_MODEL to one ending in ':free'."
        )
    if status == 404:
        return ModelUnavailable(
            f"OpenRouter does not serve {model!r}. Set OPENROUTER_MODEL to a "
            f"model id from openrouter.ai/models."
        )
    if status == 429:
        return ModelUnavailable(
            f"OpenRouter rate-limited {model!r}. Free models throttle hard; "
            f"wait, or set OPENROUTER_MODEL to a paid one."
        )
    return ModelUnavailable(f"OpenRouter returned {status}: {body[:200]}")


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
    api_key: str | None, *, model: str = DEFAULT_MODEL, provider: str = "google"
) -> LanguageModel | None:
    """Returns None when no key is configured, so callers can degrade openly.

    Deliberately not a silent stub: a pipeline that quietly runs on a fake
    model would produce an eval baseline that means nothing.
    """
    if not api_key:
        return None
    if provider == "openrouter":
        return OpenRouterModel(api_key, model=model)
    return GeminiModel(api_key, model=model)


def build_from_settings(settings: Any) -> LanguageModel | None:
    """Build whichever provider `LLM_PROVIDER` selects.

    One call site for provider choice, so switching is a .env change rather
    than an edit anywhere in the agent loop -- which is the whole claim the
    seam makes, now tested against a real outage rather than a hypothetical.
    """
    provider = settings.llm_provider.value
    key = (
        settings.openrouter_api_key
        if provider == "openrouter"
        else settings.google_ai_api_key
    )
    return build_model(key, model=settings.llm_model, provider=provider)


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
