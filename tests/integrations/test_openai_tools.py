"""Tool calling on the wire: what we send, and how a response is classified.

Every test goes through an httpx2.MockTransport, built exactly like
test_openai_chat.py's. The autouse fixture in tests/conftest.py makes the real
transport raise, so a test that forgot one fails loudly rather than spending the
developer's credit.

The SDK shapes asserted here were verified against openai 3.20.0 in Task 0
(plan check U2, appendix A).
"""

import json

import httpx2
import pytest

from app.integrations.openai import (
    ChatMessage,
    ChatOutcome,
    ToolCallRequest,
    ToolSpec,
)
from app.integrations.openai.chat import OpenAIChatClient
from tests.integrations.fakes import FakeChatClient, ok, tool_call, wants_tools
from tests.integrations.test_openai_chat import (
    MESSAGES,
    Transport,
    build,
    chat_settings,
    completion_response,
)

LIST_DOCTORS = ToolSpec(
    name="list_doctors",
    description="List the clinic's doctors.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
SEARCH = ToolSpec(
    name="search_available_slots",
    description="Find one doctor's available times.",
    parameters={
        "type": "object",
        "properties": {"doctor_id": {"type": "string"}},
        "required": ["doctor_id"],
        "additionalProperties": False,
    },
)


def tool_completion(
    *calls: dict,
    finish_reason: str = "tool_calls",
    content: str | None = None,
    prompt_tokens: int = 31,
    completion_tokens: int = 12,
) -> httpx2.Response:
    """A Chat Completions body whose assistant message asks for tools."""
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1730000000,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": list(calls),
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def fn(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def sent(transport: Transport, index: int = -1) -> dict:
    return json.loads(transport.requests[index].content)


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


async def test_tools_are_sent_as_function_tools():
    transport = Transport(completion_response("a reply"))

    await build(transport).complete(MESSAGES, (LIST_DOCTORS, SEARCH))

    assert sent(transport)["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_doctors",
                "description": "List the clinic's doctors.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_available_slots",
                "description": "Find one doctor's available times.",
                "parameters": {
                    "type": "object",
                    "properties": {"doctor_id": {"type": "string"}},
                    "required": ["doctor_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


async def test_nothing_that_steers_tool_choice_is_sent():
    """parallel_tool_calls, tool_choice and strict are deliberately absent.

    Some models reject them, and our Pydantic validation - not OpenAI's - is the
    authority on arguments (hard rule 3). The server default for parallel calls
    is fine, because the loop answers every call it is given.
    """
    transport = Transport(completion_response("a reply"))

    await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    body = sent(transport)
    for key in ("parallel_tool_calls", "tool_choice", "function_call", "functions"):
        assert key not in body, key
    assert "strict" not in json.dumps(body["tools"])


async def test_a_call_without_tools_sends_no_tools_key():
    """VS-005's body, byte for byte.

    The `tools` key is added only when there are tools, so the AI-reply path
    that has worked since VS-005 sends exactly what it used to. The cheapest way
    to break a working integration is to tidy the request shape.
    """
    transport = Transport(completion_response("a reply"))

    await build(transport).complete(MESSAGES)

    body = sent(transport)
    assert "tools" not in body
    assert body == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "you are a receptionist"},
            {"role": "user", "content": "patient sentinel text"},
        ],
        "max_completion_tokens": 1000,
        "store": False,
    }


async def test_tool_calls_and_tool_results_are_sent_back_in_openais_shape():
    """The wire shape the SDK round-trips unchanged (plan check U2).

    This is what the loop's second and third model calls look like, and getting
    it wrong is a 400 from OpenAI rather than a wrong answer - so it is pinned
    against the exact bytes.
    """
    transport = Transport(completion_response("a reply"))
    history = [
        ChatMessage("system", "you are a receptionist"),
        ChatMessage("user", "is Dr. Karim free?"),
        ChatMessage(
            "assistant", None, tool_calls=(ToolCallRequest("call_1", "list_doctors", "{}"),)
        ),
        ChatMessage("tool", '{"doctors":[]}', tool_call_id="call_1"),
    ]

    await build(transport).complete(history, (LIST_DOCTORS,))

    assert sent(transport)["messages"] == [
        {"role": "system", "content": "you are a receptionist"},
        {"role": "user", "content": "is Dr. Karim free?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_doctors", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"doctors":[]}'},
    ]


async def test_interim_text_is_sent_back_alongside_the_tool_calls():
    """ "Let me check" plus a call is a shape the model really produces, and the
    assistant turn must be reproduced as the model said it."""
    transport = Transport(completion_response("a reply"))
    history = [
        ChatMessage("user", "is Dr. Karim free?"),
        ChatMessage(
            "assistant", "Let me check.", tool_calls=(ToolCallRequest("c1", "list_doctors", "{}"),)
        ),
        ChatMessage("tool", "{}", tool_call_id="c1"),
    ]

    await build(transport).complete(history, (LIST_DOCTORS,))

    assert sent(transport)["messages"][1]["content"] == "Let me check."


# --------------------------------------------------------------------------
# The response
# --------------------------------------------------------------------------


async def test_parallel_tool_calls_are_parsed_in_order():
    transport = Transport(
        tool_completion(
            fn("call_1", "list_doctors", "{}"),
            fn("call_2", "search_available_slots", '{"doctor_id":"doc_karim"}'),
        )
    )

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS, SEARCH))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.reason == "ok"
    assert result.text is None
    assert [c.id for c in result.tool_calls] == ["call_1", "call_2"]
    assert [c.name for c in result.tool_calls] == ["list_doctors", "search_available_slots"]
    assert result.tool_calls[1].arguments == '{"doctor_id":"doc_karim"}'


async def test_arguments_that_are_not_json_are_passed_through_as_text():
    """The SDK does not parse or validate arguments, and neither do we here.

    "{not json" is a thing the model really produces. It is a perfectly good
    string, so it is NOT malformed at this layer: the registry answers it with
    an `invalid_json` tool error the model can act on. Rejecting the whole turn
    would turn a recoverable mistake into a fallback reply.
    """
    transport = Transport(tool_completion(fn("call_1", "list_doctors", "{not json")))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.tool_calls[0].arguments == "{not json"


async def test_an_unknown_tool_name_is_not_malformed():
    """Same reasoning: a name is a string. The registry answers `unknown_tool`."""
    transport = Transport(tool_completion(fn("call_1", "delete_everything", "{}")))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.tool_calls[0].name == "delete_everything"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param({"id": "x", "type": "mcp", "mcp": {}}, id="unknown-type"),
        pytest.param(
            {"id": "y", "type": "custom", "custom": {"name": "c", "input": "i"}}, id="custom"
        ),
    ],
)
async def test_a_tool_call_that_is_not_a_function_call_is_permanent(call):
    """Verified in 3.20.0: neither of these is rejected by the SDK.

    An unknown `type` builds a function-call object whose `.function` is None,
    and `type: "custom"` builds a class with no `.function` at all. There is no
    function to call and no `tool` message shape OpenAI would accept in reply,
    so the turn ends rather than guessing.
    """
    transport = Transport(tool_completion(call))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_malformed_tool_call"
    assert result.tool_calls == ()
    assert result.text is None
    # The call happened and was billed, so the counts are still reported.
    assert result.prompt_tokens == 31


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            {"type": "function", "function": {"name": "d", "arguments": "{}"}}, id="no-id"
        ),
        pytest.param(
            {"id": "", "type": "function", "function": {"name": "d", "arguments": "{}"}},
            id="blank-id",
        ),
        pytest.param(
            {"id": "a", "type": "function", "function": {"arguments": "{}"}}, id="no-name"
        ),
        pytest.param(
            {"id": "a", "type": "function", "function": {"name": "", "arguments": "{}"}},
            id="blank-name",
        ),
        pytest.param({"id": "a", "type": "function", "function": {"name": "d"}}, id="no-arguments"),
    ],
)
async def test_a_tool_call_missing_its_id_name_or_arguments_is_permanent(call):
    """`tool_call_id` and the registry lookup both need real strings.

    Verified in 3.20.0: a function call with no `arguments` key parses with
    `arguments is None`, so this is a real shape rather than a hypothetical.
    """
    transport = Transport(tool_completion(call))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_malformed_tool_call"


async def test_duplicate_tool_call_ids_are_permanent():
    """Two tool messages with one id is a request OpenAI rejects, and we could
    not tell which answer belonged to which call."""
    transport = Transport(
        tool_completion(fn("same", "list_doctors", "{}"), fn("same", "list_doctors", "{}"))
    )

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_malformed_tool_call"


async def test_tool_calls_with_finish_reason_stop_are_still_tool_calls():
    """Verified to parse in 3.20.0. The CALLS decide, not the label.

    Treating the label as authoritative would drop a real tool call on any model
    that reports `stop`, and the turn would answer from nothing.
    """
    transport = Transport(tool_completion(fn("call_1", "list_doctors", "{}"), finish_reason="stop"))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.SUCCESS
    assert len(result.tool_calls) == 1


async def test_a_truncated_tool_call_is_permanent():
    """`length` is checked BEFORE the tool calls, and that order is deliberate.

    Truncated arguments are not arguments: they are the front half of a JSON
    object. Running a tool on half its arguments is worse than failing.
    """
    transport = Transport(
        tool_completion(
            fn("call_1", "search_available_slots", '{"doctor_id":"doc_k'), finish_reason="length"
        )
    )

    result = await build(transport).complete(MESSAGES, (SEARCH,))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_reply_truncated"
    assert result.tool_calls == ()


async def test_text_alongside_tool_calls_is_kept_but_the_turn_is_a_tool_turn():
    transport = Transport(
        tool_completion(fn("call_1", "list_doctors", "{}"), content="Let me check.")
    )

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.text == "Let me check."
    assert len(result.tool_calls) == 1


async def test_blank_text_alongside_tool_calls_becomes_none():
    """So the loop never has to distinguish "" from None when deciding whether
    the model actually said anything."""
    transport = Transport(tool_completion(fn("call_1", "list_doctors", "{}"), content="   "))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.text is None


async def test_a_tool_turn_reports_its_token_counts():
    """The turn is billed per model call, and agent_runs sums these."""
    transport = Transport(
        tool_completion(fn("call_1", "list_doctors", "{}"), prompt_tokens=120, completion_tokens=8)
    )

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert (result.prompt_tokens, result.completion_tokens) == (120, 8)


async def test_an_empty_tool_calls_list_is_not_a_tool_turn():
    """`"tool_calls": []` with text is an ordinary reply, not a malformed turn."""
    transport = Transport(tool_completion(finish_reason="stop", content="just words"))

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.text == "just words"
    assert result.tool_calls == ()


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


async def test_no_reason_or_repr_carries_tool_arguments():
    """Hard rule 8. Arguments are model-written and can quote the patient.

    `reason` reaches log lines and dead_letter_jobs.error, and reprs reach
    pytest output and tracebacks. Both are checked with sentinels in an
    argument value AND in a tool name, because an unknown name is model-written
    too.
    """
    sentinel_args = '{"note":"SENTINELVALUE-my-knee-hurts"}'
    transport = Transport(
        tool_completion(fn("call_1", "SENTINELNAME_tool", sentinel_args), finish_reason="length")
    )

    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    assert "SENTINEL" not in result.reason
    assert "SENTINEL" not in repr(result)

    # And on the success path, where the call is actually carried.
    transport = Transport(tool_completion(fn("call_1", "SENTINELNAME_tool", sentinel_args)))
    result = await build(transport).complete(MESSAGES, (LIST_DOCTORS,))

    call = result.tool_calls[0]
    assert call.arguments == sentinel_args  # carried, so the registry can answer it
    assert "SENTINEL" not in repr(call)
    assert "SENTINEL" not in repr(result)
    assert "SENTINEL" not in repr(ChatMessage("assistant", None, tool_calls=(call,)))
    assert "SENTINEL" not in repr(ChatMessage("tool", sentinel_args, tool_call_id="call_1"))
    assert "call_1" in repr(call)  # an id IS safe: it is OpenAI's, not the patient's


# --------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------


async def test_the_fake_chat_client_records_the_tools_it_was_given():
    """`tool_specs` is parallel to `calls`, so every VS-005 assertion on `calls`
    keeps working unchanged."""
    fake = FakeChatClient(wants_tools(tool_call("list_doctors", {})), ok())

    first = await fake.complete([ChatMessage("user", "hi")], (LIST_DOCTORS, SEARCH))
    second = await fake.complete([ChatMessage("user", "hi")])

    assert len(fake.calls) == len(fake.tool_specs) == 2
    assert [spec.name for spec in fake.tool_specs[0]] == ["list_doctors", "search_available_slots"]
    assert fake.tool_specs[1] == []
    assert first.tool_calls[0].name == "list_doctors"
    assert second.tool_calls == ()


def test_the_tool_call_helper_accepts_a_dict_or_raw_text():
    """The model sends JSON TEXT, so "{not json" must be expressible."""
    assert tool_call("list_doctors", {}).arguments == "{}"
    assert tool_call("list_doctors", "{not json").arguments == "{not json"
    assert tool_call("x", {}, "custom-id").id == "custom-id"


def test_the_fake_still_satisfies_the_chat_client_protocol():
    from app.integrations.openai import ChatClient

    assert isinstance(FakeChatClient(), ChatClient)
    assert isinstance(
        OpenAIChatClient(
            chat_settings(),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(Transport())),
        ),
        ChatClient,
    )
