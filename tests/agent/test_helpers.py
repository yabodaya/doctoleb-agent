"""The tool-protocol helper, tested against its own failure cases.

`assert_tool_protocol` is what Tasks 7-9 rely on to prove the loop answers every
tool call. A helper that could not fail would make all of them pass for the
wrong reason, so it is checked in both directions here.
"""

import pytest

from app.integrations.openai import ChatMessage, ToolCallRequest
from tests.agent.helpers import assert_tool_protocol


def call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(call_id, "list_doctors", "{}")


def test_a_well_formed_tool_conversation_passes():
    assert_tool_protocol(
        [
            [ChatMessage("system", "prompt"), ChatMessage("user", "hi")],
            [
                ChatMessage("user", "hi"),
                ChatMessage("assistant", None, tool_calls=(call("a"), call("b"))),
                ChatMessage("tool", "{}", tool_call_id="a"),
                ChatMessage("tool", "{}", tool_call_id="b"),
                ChatMessage("assistant", "here you go"),
            ],
        ]
    )


def test_an_unanswered_tool_call_fails():
    with pytest.raises(AssertionError, match="expected"):
        assert_tool_protocol(
            [
                [
                    ChatMessage("assistant", None, tool_calls=(call("a"), call("b"))),
                    ChatMessage("tool", "{}", tool_call_id="a"),
                ]
            ]
        )


def test_answers_in_the_wrong_order_fail():
    """OpenAI matches by id, but the order is how our loop keeps `sequence`
    meaningful, so drift is worth catching."""
    with pytest.raises(AssertionError, match="expected"):
        assert_tool_protocol(
            [
                [
                    ChatMessage("assistant", None, tool_calls=(call("a"), call("b"))),
                    ChatMessage("tool", "{}", tool_call_id="b"),
                    ChatMessage("tool", "{}", tool_call_id="a"),
                ]
            ]
        )


def test_a_tool_message_answering_nothing_fails():
    with pytest.raises(AssertionError, match="answers no call"):
        assert_tool_protocol(
            [[ChatMessage("user", "hi"), ChatMessage("tool", "{}", tool_call_id="a")]]
        )


def test_a_tool_message_without_content_fails():
    with pytest.raises(AssertionError, match="no content"):
        assert_tool_protocol(
            [
                [
                    ChatMessage("assistant", None, tool_calls=(call("a"),)),
                    ChatMessage("tool", None, tool_call_id="a"),
                ]
            ]
        )
