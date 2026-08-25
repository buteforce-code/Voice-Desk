"""What the model actually sees.

This file exists because nothing tested it, and the gap cost the project its
headline metric. `Message.tool_calls` was declared, never populated, and never
converted -- so the agent's own decision to call a tool was absent from the
history it read back, and booking accuracy sat at 0.0% across 174 runs for a
reason no eval case could name. See PROJECT.md D23.

Every assertion here is about the shape on the wire, which is the one thing
`ScriptedModel` cannot exercise: it takes a `history` argument and ignores it.
"""

from __future__ import annotations

import json

import pytest

from voicedesk.llm import Message, ToolCall, ToolResult, _to_gemini, _to_openai


def a_history() -> list[Message]:
    """One turn that called a tool -- the shape the whole defect was about."""
    return [
        Message(role="agent", text="This is an automated assistant."),
        Message(role="caller", text="Book me a cardiology slot."),
        Message(
            role="agent",
            tool_calls=(
                ToolCall("find_slots", {"specialty": "Cardiology"}, call_id="c1"),
            ),
        ),
        Message(
            role="tool",
            tool_results=(
                ToolResult("find_slots", {"slots": [{"slot_id": "abc"}]}, call_id="c1"),
            ),
        ),
        Message(role="agent", text="Wednesday at nine, with Dr Fictional."),
    ]


# ==========================================================================
# The turn that was missing
# ==========================================================================


def test_openai_records_the_assistant_turn_that_called_the_tool() -> None:
    messages = _to_openai("system", a_history())

    calling = [m for m in messages if m.get("tool_calls")]
    assert calling, "the agent's own tool call is absent from its history"
    assert calling[0]["role"] == "assistant"
    assert calling[0]["tool_calls"][0]["function"]["name"] == "find_slots"


def test_gemini_records_the_model_turn_that_called_the_tool() -> None:
    contents = _to_gemini(a_history())

    calling = [
        c for c in contents if any("function_call" in p for p in c["parts"])
    ]
    assert calling, "the agent's own tool call is absent from its history"
    assert calling[0]["role"] == "model"


# ==========================================================================
# Pairing. An OpenAI-compatible endpoint rejects an unpaired result outright.
# ==========================================================================


def test_every_result_pairs_with_a_call_that_precedes_it() -> None:
    messages = _to_openai("system", a_history())

    offered: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or ():
            offered.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in offered, (
                f"result {message['tool_call_id']} pairs with no preceding call"
            )


def test_arguments_are_json_encoded_not_a_dict() -> None:
    """The OpenAI shape wants a string here. A dict is accepted by some
    gateways and dropped by others, which is the worst of both."""
    messages = _to_openai("system", a_history())
    call = next(m for m in messages if m.get("tool_calls"))["tool_calls"][0]

    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"specialty": "Cardiology"}


# ==========================================================================
# The regression that the first fix introduced
# ==========================================================================


@pytest.mark.parametrize("convert", [
    lambda h: _to_openai("system", h),
    _to_gemini,
])
def test_no_tool_call_is_ever_rendered_into_the_content_channel(convert) -> None:
    """Text-rendering the calling convention taught the model to SPEAK it.

    `<tool_calls>find_slots({...})</tool_calls>` in an assistant turn is
    indistinguishable, to a model reading its own history, from an example of
    how to talk. It stopped calling `find_slots` and started saying the tag
    out loud, in Tamil, to the caller. Text is a different field from a tool
    call on every provider behind this seam; keeping them apart is the fix.
    """
    blob = json.dumps(convert(a_history()), default=str)

    assert "<tool_calls>" not in blob
    assert "<tool_results>" not in blob


def test_a_tool_result_is_not_labelled_as_something_the_caller_said() -> None:
    """Results used to arrive as `role: user`.

    In a system whose central doctrine is that caller audio is untrusted and
    tool output is not, handing the model trusted data wearing the untrusted
    party's label inverts the exact boundary the design is built around.
    """
    messages = _to_openai("system", a_history())
    results = [m for m in messages if m["role"] == "tool"]

    assert results, "no tool-role message produced"
    user_content = " ".join(
        str(m.get("content") or "") for m in messages if m["role"] == "user"
    )
    assert "slot_id" not in user_content


# ==========================================================================
# The plain turns still survive the trip
# ==========================================================================


def test_speech_still_reaches_the_model_as_speech() -> None:
    messages = _to_openai("system", a_history())

    assert messages[0]["role"] == "system"
    assert any(
        m["role"] == "user" and m.get("content") == "Book me a cardiology slot."
        for m in messages
    )
    assert any(
        m["role"] == "assistant"
        and m.get("content") == "Wednesday at nine, with Dr Fictional."
        for m in messages
    )


# ==========================================================================
# The Gemini path, which no live call can currently check
# ==========================================================================


def test_the_gemini_contents_validate_against_the_sdks_own_types() -> None:
    """Google AI Studio 403s this project (D16/PROJECT.md), so the OpenRouter
    path is the only one a run exercises. That makes the Gemini converter
    exactly the kind of code that rots unwatched -- and the tool-schema
    translation bugs of 2026-08-20 are what that rot looks like when it
    surfaces: valid JSON Schema the API refused outright, invisible until one
    live call.

    Validating the dicts against `google.genai.types` is not the same as a
    live call and does not claim to be. It does catch a wrong key, a wrong
    nesting and a wrong role, which is what changed here.
    """
    types = pytest.importorskip("google.genai.types")

    contents = _to_gemini(a_history())
    parsed = [types.Content.model_validate(c) for c in contents]

    calls = [
        part.function_call
        for content in parsed
        for part in (content.parts or [])
        if part.function_call is not None
    ]
    responses = [
        part.function_response
        for content in parsed
        for part in (content.parts or [])
        if part.function_response is not None
    ]

    assert [c.name for c in calls] == ["find_slots"]
    assert calls[0].args == {"specialty": "Cardiology"}
    assert [r.name for r in responses] == ["find_slots"]
    assert responses[0].response == {"slots": [{"slot_id": "abc"}]}
