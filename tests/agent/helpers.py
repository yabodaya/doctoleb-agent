"""Assertions about the shape of a tool conversation.

OpenAI rejects a request whose history has an assistant turn with tool calls
that are not each answered by a `tool` message with the matching id, before the
next non-tool message. That is a rule about the WHOLE message list rather than
about any one message, so it lives here and is asserted on the recorded
requests rather than being re-checked ad hoc in each test.
"""

from collections.abc import Sequence

from app.integrations.openai import ChatMessage


def assert_tool_protocol(calls: Sequence[Sequence[ChatMessage]]) -> None:
    """Every assistant tool call is answered exactly once, in order.

    `calls` is a FakeChatClient's `calls`, or any list of message lists. The
    last request is the one that matters most - it carries the whole history -
    but every request is checked, because a loop that drops an answer mid-way
    would send one bad request and then recover.
    """
    for index, messages in enumerate(calls):
        _assert_one_request(index, list(messages))


def _assert_one_request(index: int, messages: list[ChatMessage]) -> None:
    where = f"request {index}"
    position = 0
    while position < len(messages):
        message = messages[position]
        position += 1
        if message.role != "assistant" or not message.tool_calls:
            assert message.role != "tool", f"{where}: a tool message answers no call"
            continue

        expected = [call.id for call in message.tool_calls]
        answered: list[str] = []
        while position < len(messages) and messages[position].role == "tool":
            answer = messages[position]
            assert answer.tool_call_id is not None, f"{where}: a tool message has no id"
            assert answer.content is not None, f"{where}: a tool message has no content"
            answered.append(answer.tool_call_id)
            position += 1
        assert answered == expected, f"{where}: answered {answered}, expected {expected}"
