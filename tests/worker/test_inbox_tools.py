"""The job runs the tool loop and records it with the reply.

VS-005's commit boundaries are unchanged. What is new is what T1b writes
alongside the reservation: one `agent_runs` row and its `tool_executions`, in a
SAVEPOINT, holding codes, counts and ids only.
"""

import logging

import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.db.enums import ConversationState, InboxStatus
from app.db.models import AgentRun, Conversation, DeadLetterJob, Message, ToolExecution
from app.db.repositories.errors import RunNotRecordedError
from app.integrations.booking import BookingError
from app.integrations.booking.fake import FakeBookingClient
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.integrations.booking_fakes import RecordingBooking
from tests.integrations.fakes import FakeChatClient, ok, tool_call, wants_tools
from tests.worker.conftest import (
    FROZEN_CLOCK,
    Meta,
    job_context,
    message_payload,
    meta_client,
    ok_response,
    store_event,
    worker_settings,
)

pytestmark = pytest.mark.db

WEDNESDAY_AFTERNOON = {
    "doctor_id": "doc_karim",
    "start": "2026-09-30T12:00",
    "end": "2026-09-30T17:00",
}


def spy() -> RecordingBooking:
    return RecordingBooking(FakeBookingClient.demo(clock=FROZEN_CLOCK))


def karim_script() -> FakeChatClient:
    """The Dr. Karim flow: list the doctors, search, answer."""
    return FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok("Dr. Karim has 14:00, 14:20, 15:40 and 16:20 free tomorrow afternoon."),
    )


async def _all(sessionmaker, model, **where):
    async with sessionmaker() as session:
        statement = sa.select(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        return list((await session.scalars(statement)).all())


async def _one(sessionmaker, model, **where):
    rows = await _all(sessionmaker, model, **where)
    assert len(rows) == 1, f"expected one {model.__name__}, found {len(rows)}"
    return rows[0]


async def _run(sessionmaker, transport, payload=None, settings=None, n=1, **ctx_overrides):
    settings = settings or worker_settings()
    event_id = await store_event(sessionmaker, payload or message_payload(n), n)
    outcome = await process_inbox_event(
        job_context(sessionmaker, meta_client(transport, settings), settings, **ctx_overrides),
        str(event_id),
    )
    return event_id, outcome


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_a_tool_turn_records_one_run_and_its_tool_executions(sessionmaker_for):
    chat = karim_script()
    booking = spy()

    event_id, outcome = await _run(sessionmaker_for, Meta(), chat=chat, booking=booking)

    assert outcome == "replied"
    run = await _one(sessionmaker_for, AgentRun)
    assert run.outcome == "SUCCESS"
    assert run.reason == "ok"
    assert run.model_calls == 3
    assert run.inbox_event_id == event_id
    assert run.tenant_id == dbf.TENANT_A
    assert run.prompt_version == "vs006-1"
    assert (run.prompt_tokens, run.completion_tokens) == (33, 21)
    assert run.duration_ms >= 0
    assert run.job_try == 1

    tools = sorted(await _all(sessionmaker_for, ToolExecution), key=lambda r: r.sequence)
    assert [(t.sequence, t.model_call, t.tool_name, t.status) for t in tools] == [
        (0, 1, "list_doctors", "OK"),
        (1, 2, "search_available_slots", "OK"),
    ]
    assert tools[0].argument_names == []
    assert tools[1].argument_names == ["doctor_id", "end", "start"]
    assert all(t.agent_run_id == run.id for t in tools)
    assert all(t.tenant_id == dbf.TENANT_A for t in tools)
    assert booking.methods == ["list_doctors", "search_slots"]


async def test_the_run_points_at_the_reply_it_was_committed_with(sessionmaker_for):
    _, outcome = await _run(sessionmaker_for, Meta(), chat=karim_script(), booking=spy())

    assert outcome == "replied"
    run = await _one(sessionmaker_for, AgentRun)
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    inbound = await _one(sessionmaker_for, Message, direction="INBOUND")
    assert run.reply_message_id == reply.id
    assert run.inbound_message_id == inbound.id
    assert reply.text.startswith("Dr. Karim has 14:00")


async def test_the_run_is_committed_with_the_reservation_before_the_send(
    sessionmaker_for, second_session_factory
):
    """T1b commits the reply AND its run together, before Meta is touched.

    Read from an INDEPENDENT session during the Meta call: what it sees is what
    was committed. A run written after the send would be lost whenever the send
    crashed, and the billed call would be invisible.
    """
    seen: dict = {}

    async def during_send(request):
        """Runs INSIDE the Meta call, so it sees exactly what T1b committed."""
        async with second_session_factory() as other:
            run = (await other.scalars(sa.select(AgentRun))).one_or_none()
            reply = (
                await other.scalars(sa.select(Message).where(Message.direction == "OUTBOUND"))
            ).one_or_none()
            tools = list((await other.scalars(sa.select(ToolExecution))).all())
            seen["run_id"] = run.id if run else None
            seen["reply_id"] = run.reply_message_id if run else None
            seen["matches"] = bool(run and reply and run.reply_message_id == reply.id)
            seen["tools"] = len(tools)

    await _run(
        sessionmaker_for,
        Meta(ok_response(8), hook=during_send),
        chat=karim_script(),
        booking=spy(),
    )

    assert seen["run_id"] is not None
    assert seen["matches"] is True
    assert seen["tools"] == 2


async def test_a_plain_reply_records_a_run_with_no_tool_rows(sessionmaker_for):
    """VS-005's path still records its cost. Most turns are a greeting."""
    _, outcome = await _run(sessionmaker_for, Meta(), chat=FakeChatClient(ok()))

    assert outcome == "replied"
    run = await _one(sessionmaker_for, AgentRun)
    assert run.model_calls == 1
    assert await _all(sessionmaker_for, ToolExecution) == []


# --------------------------------------------------------------------------
# Hard rule 7, across a tool call
# --------------------------------------------------------------------------


async def test_a_takeover_during_a_tool_call_is_not_blocked_and_drops_the_reply(
    sessionmaker_for, second_session_factory
):
    """Hard rule 7, in the window VS-006 opened.

    The loop can now last as long as the turn budget, so an accidental open
    transaction would block a staff takeover for up to 45 seconds. The takeover
    runs with `SET LOCAL lock_timeout = '2s'` INSIDE a tool call: if the job
    still held T1's row lock on the conversation, this fails in two seconds and
    names the bug instead of hanging the suite.

    The reply is dropped, and the run is still recorded - with a NULL reply,
    because the turn ran and was billed.
    """

    async def takeover():
        async with second_session_factory() as staff:
            await staff.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            await staff.execute(
                sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
            )
            await staff.commit()

    booking = RecordingBooking(FakeBookingClient.demo(clock=FROZEN_CLOCK), hook=takeover)
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport, chat=karim_script(), booking=booking)

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 0  # nothing was sent
    run = await _one(sessionmaker_for, AgentRun)
    assert run.reply_message_id is None
    assert run.outcome == "SUCCESS"
    assert len(await _all(sessionmaker_for, ToolExecution)) == 2


# --------------------------------------------------------------------------
# The failure paths
# --------------------------------------------------------------------------


async def test_hitting_the_model_call_limit_sends_the_fallback_and_dead_letters(
    sessionmaker_for,
):
    """D4's count limit - six since VS-007's V7 - seen from the job. PERMANENT, so
    the fallback goes out on the first try rather than after five."""
    chat = FakeChatClient(wants_tools(tool_call("list_doctors", {})))
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport, chat=chat, booking=spy())

    assert outcome == "replied_fallback"
    assert transport.sends == 1
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text == worker_settings().agent_fallback_reply
    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert letter.error == "agent_max_model_calls"
    run = await _one(sessionmaker_for, AgentRun)
    assert run.outcome == "PERMANENT"
    assert run.model_calls == 6
    tools = sorted(await _all(sessionmaker_for, ToolExecution), key=lambda r: r.sequence)
    assert [t.status for t in tools] == ["OK"] * 5 + ["SKIPPED"]
    assert tools[-1].error_code == "max_model_calls"


async def test_a_turn_deadline_retries_and_records_nothing_until_t1b(sessionmaker_for, caplog):
    """Q1, documented by a test.

    A RETRYABLE turn with tries left raises BEFORE T1b, so its run is never
    written - even though its model calls were billed and its tools ran. That
    gap is a known follow-up, not an oversight, and this is what says so.
    """

    async def slow(messages):
        import asyncio

        await asyncio.sleep(5)

    settings = worker_settings(agent_turn_timeout_seconds=0.05)
    chat = FakeChatClient(ok(), hook=slow)
    transport = Meta()

    # arq's own Retry, raised by the envelope: the job is deferred, not failed.
    with caplog.at_level(logging.WARNING), pytest.raises(Retry):
        await _run(sessionmaker_for, transport, settings=settings, chat=chat, booking=spy())

    retrying = [r.getMessage() for r in caplog.records if "inbox event retrying" in r.getMessage()]
    assert any("reason=agent_turn_timeout" in line for line in retrying)
    assert transport.sends == 0
    assert await _all(sessionmaker_for, AgentRun) == []
    assert await _all(sessionmaker_for, DeadLetterJob) == []


async def test_a_turn_deadline_on_the_last_try_sends_the_fallback_and_records_the_run(
    sessionmaker_for,
):
    """The other side of Q1: once there are no tries left, T1b IS reached, and
    the billed turn is finally recorded."""

    async def slow(messages):
        import asyncio

        await asyncio.sleep(5)

    settings = worker_settings(agent_turn_timeout_seconds=0.05)
    chat = FakeChatClient(ok(), hook=slow)
    transport = Meta()

    _, outcome = await _run(
        sessionmaker_for,
        transport,
        settings=settings,
        chat=chat,
        booking=spy(),
        job_try=settings.job_max_tries,
    )

    assert outcome == "replied_fallback"
    run = await _one(sessionmaker_for, AgentRun)
    assert run.outcome == "RETRYABLE"
    assert run.reason == "agent_turn_timeout"
    assert run.model_calls == 1
    assert run.job_try == settings.job_max_tries
    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert letter.error == "agent_turn_timeout"


async def test_a_crashing_tool_sends_the_fallback_and_dead_letters(sessionmaker_for):
    """Q6. A bug in our code produces a dead letter rather than a stranded job,
    and the patient still gets an answer."""

    class Boom(RuntimeError):
        pass

    booking = RecordingBooking(
        FakeBookingClient.demo(clock=FROZEN_CLOCK), raises=Boom("SENTINEL-internal")
    )
    chat = FakeChatClient(wants_tools(tool_call("list_doctors", {})), ok())
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport, chat=chat, booking=booking)

    assert outcome == "replied_fallback"
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text == worker_settings().agent_fallback_reply
    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert letter.error == "agent_tool_crashed"
    assert "SENTINEL" not in str(letter.payload)
    run = await _one(sessionmaker_for, AgentRun)
    assert run.outcome == "PERMANENT"
    tool = await _one(sessionmaker_for, ToolExecution)
    assert tool.error_code == "tool_crashed"
    assert tool.tool_name == "list_doctors"


async def test_a_booking_outage_still_produces_an_answer_and_records_the_error(sessionmaker_for):
    """The Booking Service being down is not a failed turn: the model is told,
    and it tells the patient honestly."""
    booking = RecordingBooking(
        FakeBookingClient.demo(clock=FROZEN_CLOCK), raises=BookingError("UNAVAILABLE")
    )
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        ok("I could not check just now; the clinic team will get back to you."),
    )

    _, outcome = await _run(sessionmaker_for, Meta(), chat=chat, booking=booking)

    assert outcome == "replied"
    assert await _all(sessionmaker_for, DeadLetterJob) == []
    tool = await _one(sessionmaker_for, ToolExecution)
    assert tool.status == "ERROR"
    assert tool.error_code == "booking_unavailable"


async def test_a_retry_that_finds_a_reserved_reply_calls_neither_the_model_nor_the_tools(
    sessionmaker_for,
):
    """VS-005's rule, now covering the tools too: a second try sends the STORED
    text. Nothing is regenerated, nothing is billed, and no second run is
    written."""
    transport = Meta(httpx_error(), ok_response(9))
    chat = karim_script()
    booking = spy()
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, message_payload(1), 1)

    # The 500 makes the send RETRYABLE, so the envelope defers the job with the
    # reply already reserved - which is the state this test is about.
    with pytest.raises(Retry):
        await process_inbox_event(
            job_context(
                sessionmaker_for,
                meta_client(transport, settings),
                settings,
                chat=chat,
                booking=booking,
            ),
            str(event_id),
        )

    calls_before, tools_before = len(chat.calls), len(booking.calls)
    second_chat = FakeChatClient(ok("this must never be sent"))
    second_booking = spy()
    outcome = await process_inbox_event(
        job_context(
            sessionmaker_for,
            meta_client(transport, settings),
            settings,
            chat=second_chat,
            booking=second_booking,
            job_try=2,
        ),
        str(event_id),
    )

    assert outcome == "replied"
    assert second_chat.calls == []  # no model call on the retry
    assert second_booking.calls == []  # and no tool call
    assert (len(chat.calls), len(booking.calls)) == (calls_before, tools_before)
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text.startswith("Dr. Karim has 14:00")
    # One run, from the first try. The retry recorded nothing.
    run = await _one(sessionmaker_for, AgentRun)
    assert run.job_try == 1


async def test_a_failed_run_recording_does_not_block_the_reply(
    sessionmaker_for, monkeypatch, caplog
):
    """The reason `_record_run` is inside a SAVEPOINT and catches.

    Bookkeeping that fails must never cost a patient their reply. The log line
    carries the exception CLASS name and nothing else.
    """
    from app.db.repositories import AgentRunRepository

    async def explode(self, run):
        raise RunNotRecordedError("IntegrityError")

    monkeypatch.setattr(AgentRunRepository, "add", explode)
    transport = Meta()

    with caplog.at_level(logging.ERROR):
        _, outcome = await _run(sessionmaker_for, transport, chat=karim_script(), booking=spy())

    assert outcome == "replied"
    assert transport.sends == 1
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text.startswith("Dr. Karim has 14:00")
    assert await _all(sessionmaker_for, AgentRun) == []
    lines = [r.getMessage() for r in caplog.records]
    assert any("agent run not recorded" in line and "IntegrityError" in line for line in lines)


# --------------------------------------------------------------------------
# What the log line carries
# --------------------------------------------------------------------------


async def test_the_generation_log_line_carries_counts_and_codes_only(sessionmaker_for, caplog):
    with caplog.at_level(logging.INFO):
        event_id, _ = await _run(sessionmaker_for, Meta(), chat=karim_script(), booking=spy())

    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("reply generated")
    )
    assert f"event_id={event_id}" in line
    for fragment in (
        "outcome=SUCCESS",
        "reason=ok",
        "prompt_version=vs006-1",
        "model_calls=3",
        "tool_calls=2",
        "tool_errors=0",
        "prompt_tokens=33",
        "completion_tokens=21",
        "duration_ms=",
    ):
        assert fragment in line, fragment
    # No tool NAMES: an unknown one is model-written (the Q9 reasoning).
    for name in ("list_doctors", "search_available_slots", "doc_karim"):
        assert name not in line, name


async def test_the_log_line_counts_tool_errors(sessionmaker_for, caplog):
    chat = FakeChatClient(
        wants_tools(tool_call("search_available_slots", {"doctor_id": "doc_karim"})),
        ok(),
    )

    with caplog.at_level(logging.INFO):
        await _run(sessionmaker_for, Meta(), chat=chat, booking=spy())

    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("reply generated")
    )
    assert "tool_calls=1" in line
    assert "tool_errors=1" in line


async def test_no_log_line_contains_tool_arguments_results_or_doctor_names(
    sessionmaker_for, caplog
):
    """Hard rule 8, across the whole job. Sentinels in the patient's text, in a
    tool argument, in an unknown tool name, and in the model's reply."""
    payload = message_payload(1, text={"body": "SENTINELPATIENT knee"})
    chat = FakeChatClient(
        wants_tools(
            tool_call("SENTINELTOOL_nope", {"note": "SENTINELARG"}, "c1"),
            tool_call("list_doctors", {}, "c2"),
        ),
        ok("SENTINELREPLY at 14:00"),
    )

    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker_for, Meta(), payload=payload, chat=chat, booking=spy())

    rendered = "\n".join(r.getMessage() for r in caplog.records)
    for sentinel in ("SENTINELPATIENT", "SENTINELARG", "SENTINELTOOL", "SENTINELREPLY"):
        assert sentinel not in rendered, sentinel
    # And the clinic's own data does not leak either.
    for clinic_data in ("Karim", "Haddad", "doc_karim", "Demo Street"):
        assert clinic_data not in rendered, clinic_data


async def test_nothing_sensitive_reaches_the_two_new_tables(sessionmaker_for):
    """The tables hold codes, counts and ids. An unknown tool name is stored as
    "unknown", and only DECLARED argument names are stored."""
    chat = FakeChatClient(
        wants_tools(
            tool_call("SENTINELTOOL_nope", {"SENTINELKEY": 1}, "c1"),
            tool_call("search_available_slots", {**WEDNESDAY_AFTERNOON, "SENTINELKEY": 1}, "c2"),
        ),
        ok("SENTINELREPLY"),
    )

    await _run(sessionmaker_for, Meta(), chat=chat, booking=spy())

    tools = sorted(await _all(sessionmaker_for, ToolExecution), key=lambda r: r.sequence)
    run = await _one(sessionmaker_for, AgentRun)
    rendered = (
        "".join(f"{t.tool_name}{t.argument_names}{t.status}{t.error_code}" for t in tools)
        + f"{run.reason}{run.outcome}{run.model}{run.prompt_version}"
    )
    assert "SENTINEL" not in rendered
    assert tools[0].tool_name == "unknown"
    assert tools[0].argument_names == []
    # The second call was rejected by extra="forbid", so only the declared
    # names that were present are stored.
    assert tools[1].argument_names == ["doctor_id", "end", "start"]
    assert tools[1].status == "INVALID_ARGUMENTS"


async def test_the_recorded_model_is_the_configured_one(sessionmaker_for):
    """Q10: the CONFIGURED OPENAI_CHAT_MODEL, not the snapshot the response
    reports serving. NULL when unset - "the turn never reached OpenAI"."""
    settings = worker_settings(openai_chat_model="test-model")

    await _run(sessionmaker_for, Meta(), settings=settings, chat=FakeChatClient(ok()))

    run = await _one(sessionmaker_for, AgentRun)
    assert run.model == "test-model"


async def test_the_inbox_row_is_processed_whatever_the_run_recording_did(sessionmaker_for):
    from app.db.models import WebhookInbox

    event_id, _ = await _run(sessionmaker_for, Meta(), chat=karim_script(), booking=spy())

    inbox = await _one(sessionmaker_for, WebhookInbox, id=event_id)
    assert inbox.status == InboxStatus.PROCESSED.value


def httpx_error():
    import httpx

    return httpx.Response(500, json={"error": {"message": "upstream"}})
