"""The tool loop: what bounds it, what it records, and what it never sends.

No database, no network. A `RecordingBooking` over the demo fake, a frozen
clock, and a `FakeChatClient` scripted with exactly the responses each case
needs.

The loop is where the model's requests meet our decisions, so most of these
tests are about the decisions holding when the model behaves badly: asking
forever, asking for a tool that does not exist, sending arguments that are not
JSON, or sending a tenant it invented.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime

import pytest

from app.agent import AgentRuntime, Turn, build_messages, process_turn
from app.agent.loop import MAX_MODEL_CALLS, MAX_TOOL_CALLS_PER_TURN
from app.agent.prompts import SYSTEM_PROMPT
from app.db.enums import MessageModality
from app.integrations.booking import BookingError
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.openai import ChatOutcome, ChatResult
from tests.agent.helpers import assert_tool_protocol
from tests.integrations.booking_fakes import RecordingBooking
from tests.integrations.fakes import (
    AI_REPLY,
    FakeChatClient,
    ok,
    permanent,
    retryable,
    tool_call,
    wants_tools,
)

NOW = datetime(2026, 9, 29, 7, tzinfo=UTC)  # Tuesday 29 Sep 2026, 10:00 local
TENANT = "clinic-alpha"
PATIENT_TEXT = "Is Dr. Karim available tomorrow afternoon?"

WEDNESDAY_AFTERNOON = {
    "doctor_id": "doc_karim",
    "start": "2026-09-30T12:00",
    "end": "2026-09-30T17:00",
}


def turn(input_text: str = PATIENT_TEXT, tenant: str = TENANT) -> Turn:
    return Turn(
        tenant_id=tenant,
        contact_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        modality=MessageModality.TEXT,
        input_text=input_text,
    )


def runtime(booking=None, *, timeout: float = 45.0, clock=None) -> AgentRuntime:
    return AgentRuntime(
        booking=booking or FakeBookingClient.demo(clock=lambda: NOW),
        clock=clock or (lambda: NOW),
        turn_timeout_seconds=timeout,
    )


def spy() -> RecordingBooking:
    return RecordingBooking(FakeBookingClient.demo(clock=lambda: NOW))


def tool_messages(chat: FakeChatClient, index: int = -1) -> list[str]:
    """The `tool` message contents of one recorded request."""
    return [m.content for m in chat.calls[index] if m.role == "tool"]


# --------------------------------------------------------------------------
# The happy paths
# --------------------------------------------------------------------------


async def test_a_turn_without_tool_calls_is_one_model_call():
    """VS-005's path, unchanged. Most turns are a greeting or a thank-you and
    must not pay for a tool round trip."""
    chat = FakeChatClient(ok())
    booking = spy()

    result = await process_turn(turn("hello"), chat, runtime(booking))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.reply_text == AI_REPLY
    assert result.model_calls == 1
    assert result.tool_calls == ()
    assert booking.calls == []
    # The tools are still OFFERED, every call: the model decides it does not
    # need them.
    assert [s.name for s in chat.tool_specs[0]] == [
        "get_clinic_information",
        "list_doctors",
        "search_available_slots",
    ]


async def test_tool_results_are_fed_back_and_the_final_text_is_the_reply():
    """The slice's goal, at the loop's level: ask, look up, answer."""
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok("Dr. Karim has 14:00, 14:20, 15:40 and 16:20 free tomorrow afternoon."),
    )
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.model_calls == 3
    assert result.reply_text.startswith("Dr. Karim has 14:00")
    assert booking.methods == ["list_doctors", "search_slots"]
    # The second request carried the doctors; the third carried the slots.
    assert "doc_karim" in tool_messages(chat, 1)[0]
    assert "2026-09-30T14:00" in tool_messages(chat, 2)[-1]
    assert [(r.sequence, r.model_call, r.tool_name, r.status.value) for r in result.tool_calls] == [
        (0, 1, "list_doctors", "OK"),
        (1, 2, "search_available_slots", "OK"),
    ]


async def test_interim_text_is_echoed_back_but_is_never_the_reply():
    """ "Let me check" plus a tool call is a shape models really produce. The
    patient gets ONE reply, and it is the final text-only answer."""
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {}), text="Let me check for you."),
        ok("Here are the times."),
    )

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.reply_text == "Here are the times."
    assistant = [m for m in chat.calls[1] if m.role == "assistant"]
    assert assistant[0].content == "Let me check for you."


async def test_parallel_tool_calls_are_all_answered_in_order():
    """One response can ask for several. They run SEQUENTIALLY and in order -
    deterministic for `sequence` and for tests - and every one gets a `tool`
    message, because OpenAI requires one per tool_call_id."""
    chat = FakeChatClient(
        wants_tools(
            tool_call("list_doctors", {}, "call_a"),
            tool_call("get_clinic_information", {}, "call_b"),
        ),
        ok(),
    )
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert booking.methods == ["list_doctors", "get_clinic"]
    assert [r.sequence for r in result.tool_calls] == [0, 1]
    assert [r.tool_name for r in result.tool_calls] == ["list_doctors", "get_clinic_information"]
    assert [r.model_call for r in result.tool_calls] == [1, 1]
    answered = [m.tool_call_id for m in chat.calls[1] if m.role == "tool"]
    assert answered == ["call_a", "call_b"]


async def test_every_request_answers_every_tool_call():
    """The rule OpenAI enforces, checked across every request of the turn.

    A missing tool message is a 400 on the next request rather than a worse
    answer, so it would look like an outage.
    """
    chat = FakeChatClient(
        wants_tools(
            tool_call("list_doctors", {}, "c1"), tool_call("get_clinic_information", {}, "c2")
        ),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON, "c3")),
        ok(),
    )

    await process_turn(turn(), chat, runtime(spy()))

    assert_tool_protocol(chat.calls)


# --------------------------------------------------------------------------
# The limits
# --------------------------------------------------------------------------


async def test_the_loop_stops_at_four_model_calls():
    """Decision D4's count limit. The model asks for tools forever.

    Exactly four calls happen; the fourth response's calls are RECORDED and NOT
    executed - running them would produce results nothing could read. PERMANENT
    (Q3), because a retry would loop the same way and bill again.
    """
    chat = FakeChatClient(wants_tools(tool_call("list_doctors", {})))
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert MAX_MODEL_CALLS == 4
    assert len(chat.calls) == 4
    assert result.model_calls == 4
    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "agent_max_model_calls"
    assert result.reply_text is None
    # Three rounds of tools ran; the fourth response's call did not.
    assert booking.methods == ["list_doctors"] * 3
    assert [r.status.value for r in result.tool_calls] == ["OK", "OK", "OK", "SKIPPED"]
    assert result.tool_calls[-1].error_code == "max_model_calls"
    assert result.tool_calls[-1].model_call == 4


async def test_more_than_twelve_tool_calls_are_skipped():
    """A second, independent cap for the parallel case: 5 calls per response
    would otherwise be 15 executions inside one turn."""
    five = [tool_call("list_doctors", {}, f"call_{n}") for n in range(5)]
    chat = FakeChatClient(wants_tools(*five))
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert MAX_TOOL_CALLS_PER_TURN == 12
    executed = [r for r in result.tool_calls if r.status.value == "OK"]
    skipped = [r for r in result.tool_calls if r.status.value == "SKIPPED"]
    assert len(executed) == 12
    assert len(booking.calls) == 12
    assert all(r.error_code in ("too_many_tool_calls", "max_model_calls") for r in skipped)
    # Every call still got an answer, capped ones included.
    assert_tool_protocol(chat.calls)


# --------------------------------------------------------------------------
# What the model gets wrong, and recovers from
# --------------------------------------------------------------------------


async def test_invalid_arguments_are_reported_to_the_model_and_the_loop_continues():
    """The slice's acceptance criterion. An invalid call is not a failed turn:
    the model is told what to fix and gets another go."""
    chat = FakeChatClient(
        wants_tools(tool_call("search_available_slots", {"doctor_id": "doc_karim"})),
        ok("Sorry, let me try that again."),
    )

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.model_calls == 2
    reported = json.loads(tool_messages(chat, 1)[0])
    assert reported["error"]["code"] == "invalid_arguments"
    assert {"argument": "start", "problem": "is required"} in reported["error"]["problems"]
    assert result.tool_calls[0].status.value == "INVALID_ARGUMENTS"


async def test_an_invalid_call_then_a_corrected_one_fits_in_four_model_calls(monkeypatch):
    """Amendment B6. The reason MAX_MODEL_CALLS is 4 rather than 3.

    The realistic recovery path is: list_doctors, a search with bad arguments,
    the same search corrected, then the answer. That is exactly four, so the
    budget accommodates one self-correction without being raised.
    """
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", {"doctor_id": "doc_karim"})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok("Dr. Karim has 14:00, 14:20, 15:40 and 16:20 free."),
    )
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.model_calls == MAX_MODEL_CALLS == 4
    assert result.reply_text.startswith("Dr. Karim has")
    assert [r.status.value for r in result.tool_calls] == ["OK", "INVALID_ARGUMENTS", "OK"]
    # The corrected search really ran: the spy saw one search, not two.
    assert booking.methods == ["list_doctors", "search_slots"]
    assert "2026-09-30T14:00" in tool_messages(chat, 3)[-1]


async def test_malformed_json_and_unknown_tools_do_not_end_the_turn():
    """Both are things the model can fix on the next call, so both are answered
    rather than ending the turn. Neither the arguments nor the invented name is
    echoed."""
    chat = FakeChatClient(
        wants_tools(
            tool_call("list_doctors", "{not json SENTINELARG", "c1"),
            tool_call("SENTINELNAME_nope", {}, "c2"),
        ),
        ok("Sorry about that."),
    )

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.model_calls == 2
    codes = [json.loads(content)["error"]["code"] for content in tool_messages(chat, 1)]
    assert codes == ["invalid_json", "unknown_tool"]
    assert "SENTINEL" not in "".join(tool_messages(chat, 1))
    assert [r.tool_name for r in result.tool_calls] == ["list_doctors", "unknown"]
    assert "SENTINEL" not in repr(result)


async def test_a_booking_failure_is_reported_and_the_turn_can_still_answer():
    """Hard rule 5's neighbourhood: the alternative to a real answer is an
    honest one, never an invented one."""
    booking = RecordingBooking(
        FakeBookingClient.demo(clock=lambda: NOW), raises=BookingError("UNAVAILABLE")
    )
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        ok("I could not check just now; the clinic team will get back to you."),
    )

    result = await process_turn(turn(), chat, runtime(booking))

    assert result.outcome is ChatOutcome.SUCCESS
    assert "Do not guess" in tool_messages(chat, 1)[0]
    assert result.tool_calls[0].status.value == "ERROR"
    assert result.tool_calls[0].error_code == "booking_unavailable"


# --------------------------------------------------------------------------
# The deadline
# --------------------------------------------------------------------------


async def test_the_turn_deadline_covers_model_calls():
    """Decision D4's wall-clock limit, on the model side.

    RETRYABLE (Q3), consistent with VS-005's openai_timeout: the job retries
    with backoff and sends the fallback on the last try.
    """

    async def slow(messages):
        await asyncio.sleep(5)

    chat = FakeChatClient(ok(), hook=slow)
    started = time.monotonic()

    result = await process_turn(turn(), chat, runtime(spy(), timeout=0.05))

    assert time.monotonic() - started < 2  # it really was cut off
    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "agent_turn_timeout"
    assert result.reply_text is None
    assert result.model_calls == 1  # started, and billed


async def test_the_turn_deadline_covers_tool_calls():
    """The other half of D4: a slow TOOL must not outlive the budget either.

    The interrupted call still gets a record. Without it the call is invisible -
    it was asked for, it started, it may have reached the Booking Service - and
    `tool_executions` would show a gap in `sequence` with nothing explaining it.
    """

    async def slow():
        await asyncio.sleep(5)

    booking = RecordingBooking(FakeBookingClient.demo(clock=lambda: NOW), hook=slow)
    chat = FakeChatClient(wants_tools(tool_call("list_doctors", {})), ok())
    started = time.monotonic()

    result = await process_turn(turn(), chat, runtime(booking, timeout=0.05))

    assert time.monotonic() - started < 2
    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "agent_turn_timeout"
    assert len(result.tool_calls) == 1
    in_flight = result.tool_calls[0]
    assert in_flight.tool_name == "list_doctors"
    assert in_flight.status.value == "ERROR"
    assert in_flight.error_code == "turn_timeout"
    assert in_flight.sequence == 0


async def test_a_timeout_error_that_is_not_the_turn_deadline_escapes():
    """Risk R11. A TimeoutError from somewhere else is a BUG, not slowness.

    Swallowing it would report a bug as "OpenAI was slow" and retry it five
    times, then send the fallback - hiding it completely.
    """

    async def raise_timeout(messages):
        raise TimeoutError("not the turn deadline")

    chat = FakeChatClient(ok(), hook=raise_timeout)

    with pytest.raises(TimeoutError, match="not the turn deadline"):
        await process_turn(turn(), chat, runtime(spy(), timeout=30))


# --------------------------------------------------------------------------
# Failures that end the turn
# --------------------------------------------------------------------------


async def test_a_model_failure_mid_loop_keeps_its_reason_and_the_records_so_far():
    """The tools that DID run were billed and are worth recording, even though
    the turn produced no reply. The job's one retry layer re-runs the whole
    turn, which is safe because every tool is read-only."""
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        retryable("openai_http_503"),
    )

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_http_503"
    assert result.model_calls == 2
    assert [r.tool_name for r in result.tool_calls] == ["list_doctors"]


async def test_a_permanent_model_failure_ends_the_turn_with_its_own_reason():
    chat = FakeChatClient(permanent("openai_insufficient_quota"))

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_insufficient_quota"


async def test_a_crashing_tool_ends_the_turn_permanently():
    """Q6. A bug in our code is not something a retry fixes, and hard rule 11
    wants a dead letter rather than a stranded job.

    The record carries the exception CLASS name only - never its message, which
    came from code that had just been handed model-written arguments.
    """

    class Boom(RuntimeError):
        pass

    booking = RecordingBooking(
        FakeBookingClient.demo(clock=lambda: NOW), raises=Boom("SENTINEL-internal-detail")
    )
    chat = FakeChatClient(wants_tools(tool_call("list_doctors", {})), ok())

    result = await process_turn(turn(), chat, runtime(booking))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "agent_tool_crashed"
    assert result.reply_text is None
    assert [r.tool_name for r in result.tool_calls] == ["list_doctors"]
    assert result.tool_calls[0].error_code == "tool_crashed"
    assert "SENTINEL" not in repr(result)


async def test_a_truncated_tool_call_never_reaches_the_loop_as_a_tool_turn():
    """Amendment B7, pinning Task 5's classification ORDER from this side.

    `read_completion` checks `finish_reason == "length"` BEFORE the tool calls,
    so a truncated response is PERMANENT and its half-written arguments never
    become a tool call. Running a tool on the front half of a JSON object is
    worse than failing.

    Constructed as the ChatResult that classification produces, so this test
    fails if the ordering is ever reversed in chat.py.
    """
    truncated = ChatResult(
        ChatOutcome.PERMANENT, "openai_reply_truncated", None, 31, 12, tool_calls=()
    )
    chat = FakeChatClient(truncated)
    booking = spy()

    result = await process_turn(turn(), chat, runtime(booking))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_reply_truncated"
    assert result.model_calls == 1
    assert result.tool_calls == ()
    assert booking.calls == []  # nothing ran on half-written arguments


# --------------------------------------------------------------------------
# Bookkeeping, and what the model never sees
# --------------------------------------------------------------------------


async def test_tokens_are_summed_across_model_calls():
    """`agent_runs` is what finally makes a tool turn's cost visible (VS-005
    follow-up 11), and a tool turn is typically three billed calls."""
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {}), prompt_tokens=100, completion_tokens=10),
        wants_tools(
            tool_call("search_available_slots", WEDNESDAY_AFTERNOON),
            prompt_tokens=200,
            completion_tokens=20,
        ),
        ok(prompt_tokens=300, completion_tokens=30),
    )

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.prompt_tokens == 600
    assert result.completion_tokens == 60


async def test_tokens_stay_none_when_the_api_reports_none():
    """So "the API told us nothing" and "zero tokens" stay distinguishable in
    agent_runs."""
    chat = FakeChatClient(ChatResult(ChatOutcome.SUCCESS, "ok", AI_REPLY))

    result = await process_turn(turn(), chat, runtime(spy()))

    assert result.prompt_tokens is None
    assert result.completion_tokens is None


async def test_the_tenant_id_is_in_no_message_sent_to_the_model():
    """Hard rule 4, as a grep over EVERYTHING the model was sent.

    Every message of every request, every tool result, and every schema. If the
    tenant were reachable, a patient could talk the model into asking for
    another clinic's data.
    """
    sentinel = "clinic-SENTINELTENANT"
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("get_clinic_information", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok(),
    )

    result = await process_turn(turn(tenant=sentinel), chat, runtime(spy()))

    for messages in chat.calls:
        for message in messages:
            assert "SENTINELTENANT" not in (message.content or "")
            for call in message.tool_calls:
                assert "SENTINELTENANT" not in call.arguments
    for specs in chat.tool_specs:
        assert "SENTINELTENANT" not in json.dumps([s.__dict__ for s in specs])
    assert "SENTINELTENANT" not in repr(result)
    for record in result.tool_calls:
        assert "SENTINELTENANT" not in repr(record)


async def test_the_clock_message_is_separate_from_the_prompt_and_just_before_the_answer():
    """Decision D3's placement.

    Separate, because the prompt's SHA-256 is pinned to its version and a prompt
    that changed every day could not be pinned. LATE, because it keeps the
    static prefix (prompt, schemas, history) identical between turns - what
    OpenAI's automatic prompt caching needs - and puts the date next to the
    question.
    """
    messages = build_messages(turn(), NOW)

    assert messages[0].role == "system"
    assert messages[0].content == SYSTEM_PROMPT
    assert "Tuesday 29 September 2026" not in SYSTEM_PROMPT
    assert messages[-2].role == "system"
    assert "Current date and time at the clinic" in messages[-2].content
    assert messages[-1].role == "user"
    assert messages[-1].content == PATIENT_TEXT


async def test_the_clock_is_read_once_per_turn():
    """A turn that read the clock twice could answer "tomorrow" with two
    different dates - at midnight, or when a slow tool call straddles it."""
    reads = []

    def counting_clock():
        reads.append(1)
        return NOW

    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok(),
    )

    await process_turn(turn(), chat, runtime(spy(), clock=counting_clock))

    assert len(reads) == 1


async def test_the_result_carries_the_prompt_version():
    result = await process_turn(turn("hello"), FakeChatClient(ok()), runtime(spy()))

    assert result.prompt_version == "vs006-1"
