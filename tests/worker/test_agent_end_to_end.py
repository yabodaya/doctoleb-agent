"""Acceptance (a): "Is Dr. Karim available tomorrow afternoon?", end to end.

A signed webhook goes in, the queue is drained, and a real answer built from
fake data comes out - through the whole stack: signature check, dedupe, inbox,
worker, tenant resolution, the tool loop, the Booking Service interface, the
reply reservation and the Meta send.

Nothing here reaches the network. The model is either a scripted `FakeChatClient`
or the REAL `OpenAIChatClient` over an `httpx2.MockTransport`, depending on
whether the point is the flow or the wire.
"""

import json
import logging

import httpx2
import pytest

from app.db.models import AgentRun, DeadLetterJob, Message, ToolExecution
from app.integrations.booking import ClinicInfo, Doctor, Location, Service
from app.integrations.booking.fake import FakeBookingClient, FakeClinic, FakeDoctor
from tests.db import factories as dbf
from tests.integrations.booking_fakes import RecordingBooking
from tests.integrations.fakes import FakeChatClient, ok, tool_call, wants_tools
from tests.whatsapp_factories import PHONE_NUMBER_ID, PROFILE_NAME, envelope, phone, text_message
from tests.worker.conftest import FROZEN_CLOCK, Meta, ok_response, wamid
from tests.worker.test_end_to_end import (  # noqa: F401 - shared harness
    OpenAI,
    _rows,
    openai_settings,
    real_chat,
)

pytestmark = pytest.mark.db

QUESTION = "Is Dr. Karim available tomorrow afternoon?"
ANSWER = (
    "Dr. Karim has 14:00, 14:20, 15:40 and 16:20 free tomorrow afternoon. "
    "The clinic team will get back to you to confirm."
)
WEDNESDAY_AFTERNOON = {
    "doctor_id": "doc_karim",
    "start": "2026-09-30T12:00",
    "end": "2026-09-30T17:00",
}


def karim_script() -> FakeChatClient:
    """The three responses the live model is expected to produce.

    1. list_doctors - D5 says the id comes from there, never from a name.
    2. search_available_slots for Wednesday 12:00-17:00 - "tomorrow" resolved
       against the clock message, "afternoon" from the tool description (B2).
    3. the answer, in plain WhatsApp English, confirming nothing.
    """
    return FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        ok(ANSWER),
    )


def spy() -> RecordingBooking:
    return RecordingBooking(FakeBookingClient.demo(clock=FROZEN_CLOCK))


async def test_is_dr_karim_available_tomorrow_afternoon(sessionmaker_for, pipeline, caplog):
    """The slice's goal, proved end to end against fake data.

    Frozen on Tuesday 29 September 2026 at 10:00 clinic local, so "tomorrow" is
    Wednesday the 30th and Dr. Karim's afternoon pattern is exactly four slots.
    """
    transport = Meta(ok_response(9))
    chat = karim_script()
    booking = spy()

    with caplog.at_level(logging.DEBUG):
        assert (
            await pipeline.post(envelope(messages=[text_message(1, body=QUESTION)]))
        ).status_code == 200
        assert await pipeline.drain(transport, chat=chat, booking=booking) == ["replied"]

    # --- the model was asked three times, and the conversation was well-formed
    assert len(chat.calls) == 3
    from tests.agent.helpers import assert_tool_protocol

    assert_tool_protocol(chat.calls)

    # --- call 1 carried the clock message, naming today and tomorrow
    clock = [m for m in chat.calls[0] if m.role == "system"][-1]
    assert "Tuesday 29 September 2026, 10:00" in clock.content
    assert "Tomorrow is Wednesday 30 September 2026" in clock.content

    # --- call 2 carried the doctors; call 3 carried exactly the four slots
    doctors = json.loads([m for m in chat.calls[1] if m.role == "tool"][0].content)
    assert [d["doctor_id"] for d in doctors["doctors"]] == [
        "doc_karim",
        "doc_rania",
        "doc_samir",
    ]
    slots = json.loads([m for m in chat.calls[2] if m.role == "tool"][-1].content)
    assert [s["start"] for s in slots["slots"]] == [
        "2026-09-30T14:00",
        "2026-09-30T14:20",
        "2026-09-30T15:40",
        "2026-09-30T16:20",
    ]
    assert all(s["day"] == "Wednesday" for s in slots["slots"])
    assert slots["more_available"] is False

    # --- the booking client saw OUR tenant and the right instants
    assert booking.methods == ["list_doctors", "search_slots"]
    assert booking.tenants == [dbf.TENANT_A, dbf.TENANT_A]
    start = booking.calls[1].kwargs["start"]
    end = booking.calls[1].kwargs["end"]
    assert start.isoformat() == "2026-09-30T12:00:00+03:00"
    assert end.isoformat() == "2026-09-30T17:00:00+03:00"

    # --- Meta was sent the model's text, once
    assert transport.sends == 1
    assert json.loads(transport.requests[0].content)["text"]["body"] == ANSWER
    reply = (await _rows(sessionmaker_for, Message, direction="OUTBOUND"))[0]
    assert reply.text == ANSWER
    assert reply.status == "SENT"

    # --- the run and its tools were recorded
    run = (await _rows(sessionmaker_for, AgentRun))[0]
    assert (run.outcome, run.reason, run.model_calls) == ("SUCCESS", "ok", 3)
    assert (run.prompt_tokens, run.completion_tokens) == (33, 21)
    assert run.reply_message_id == reply.id
    tools = sorted(await _rows(sessionmaker_for, ToolExecution), key=lambda r: r.sequence)
    assert [(t.sequence, t.tool_name, t.argument_names, t.status) for t in tools] == [
        (0, "list_doctors", [], "OK"),
        (1, "search_available_slots", ["doctor_id", "end", "start"], "OK"),
    ]
    assert await _rows(sessionmaker_for, DeadLetterJob) == []

    # --- and nothing that happened is in the logs
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (QUESTION, ANSWER, "Karim", "doc_karim", "14:00", PROFILE_NAME, phone(1)):
        assert secret not in rendered, secret


async def test_the_tool_loop_on_the_wire_never_sends_the_tenant_or_patient_identifiers(
    sessionmaker_for, pipeline
):
    """The same turn through the REAL OpenAIChatClient, scripted in OpenAI's JSON.

    A FakeChatClient cannot prove what the SDK puts on the wire. This can: every
    request carries the three tools, requests 2 and 3 echo the assistant's
    tool_calls and our tool results, and NO request body contains the tenant,
    the profile name, the phone number, a wamid or any row id.
    """

    def tool_completion(*calls: dict) -> httpx2.Response:
        return _completion(
            {"role": "assistant", "content": None, "tool_calls": list(calls)}, "tool_calls"
        )

    def fn(call_id: str, name: str, arguments: str) -> dict:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }

    openai = OpenAI(
        tool_completion(fn("call_1", "list_doctors", "{}")),
        tool_completion(fn("call_2", "search_available_slots", json.dumps(WEDNESDAY_AFTERNOON))),
        _completion({"role": "assistant", "content": ANSWER}, "stop"),
    )
    # The REAL client needs a (fake) key and model, or it short-circuits to
    # openai_api_key_unset before anything reaches the transport.
    settings = openai_settings()

    await pipeline.post(envelope(messages=[text_message(1, body=QUESTION)]))
    outcomes = await pipeline.drain(
        Meta(ok_response(9)),
        chat=real_chat(openai, settings),
        settings=settings,
        booking=spy(),
    )

    assert outcomes == ["replied"]
    assert openai.calls == 3
    bodies = openai.bodies()

    # Every request offers the same three tools, in the same order - which is
    # what OpenAI's automatic prompt caching needs.
    for body in bodies:
        assert [t["function"]["name"] for t in body["tools"]] == [
            "get_clinic_information",
            "list_doctors",
            "search_available_slots",
        ]
        assert all(t["type"] == "function" for t in body["tools"])

    # Request 2 echoes the assistant turn and answers its call; request 3 does
    # the same for the search.
    assert bodies[1]["messages"][-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [fn("call_1", "list_doctors", "{}")],
    }
    assert bodies[1]["messages"][-1]["role"] == "tool"
    assert bodies[1]["messages"][-1]["tool_call_id"] == "call_1"
    assert bodies[2]["messages"][-1]["tool_call_id"] == "call_2"
    assert "2026-09-30T14:00" in bodies[2]["messages"][-1]["content"]

    # Nothing about the patient, the clinic's key, or our own rows.
    inbound = (await _rows(sessionmaker_for, Message, direction="INBOUND"))[0]
    rendered = json.dumps(bodies)
    for secret in (
        dbf.TENANT_A,
        PROFILE_NAME,
        phone(1),
        wamid(1),
        PHONE_NUMBER_ID,
        str(inbound.id),
        str(inbound.conversation_id),
    ):
        assert secret not in rendered, secret


async def test_the_same_webhook_delivered_twice_runs_the_loop_once(sessionmaker_for, pipeline):
    """Hard rule 2, now with tools: the dedupe gate is what stops a duplicate
    delivery costing a second billed turn and a second set of tool calls."""
    transport = Meta(ok_response(9))
    chat = karim_script()
    booking = spy()
    body = envelope(messages=[text_message(1, body=QUESTION)])

    assert (await pipeline.post(body)).status_code == 200
    assert (await pipeline.post(body)).status_code == 200
    outcomes = await pipeline.drain(transport, chat=chat, booking=booking)

    assert outcomes[0] == "replied"
    assert len(chat.calls) == 3  # not six
    assert booking.methods == ["list_doctors", "search_slots"]
    assert transport.sends == 1
    assert len(await _rows(sessionmaker_for, Message, direction="OUTBOUND")) == 1
    assert len(await _rows(sessionmaker_for, AgentRun)) == 1
    assert len(await _rows(sessionmaker_for, ToolExecution)) == 2


async def test_nothing_sensitive_reaches_logs_job_results_dead_letters_or_the_agent_tables(
    sessionmaker_for, pipeline, caplog
):
    """Hard rule 8, swept across every channel at once.

    Sentinels in: the patient's text, a tool argument, an unknown tool name, a
    DOCTOR's name served by a custom clinic, and the model's own words. The turn
    is made to fail with a crash, so a dead letter exists to check too.
    """
    sentinel_clinic = FakeClinic(
        info=ClinicInfo(
            name="SENTINELCLINIC Demo",
            timezone="Asia/Beirut",
            locations=(Location(name="Main", address="1 SENTINELSTREET, Beirut"),),
        ),
        doctors=(
            FakeDoctor(
                doctor=Doctor(
                    doctor_id="doc_sentinel",
                    name="Dr. SENTINELDOCTOR Demo",
                    specialty="General practice",
                    services=(Service(service_id="s", name="Consultation", duration_minutes=20),),
                ),
                starts={2: ()},
                slot_minutes=20,
            ),
        ),
    )

    class Boom(RuntimeError):
        pass

    booking = RecordingBooking(
        FakeBookingClient(clinics={dbf.TENANT_A: sentinel_clinic}, clock=FROZEN_CLOCK),
        raises=Boom("SENTINELEXCEPTION detail"),
    )
    chat = FakeChatClient(
        wants_tools(
            tool_call("SENTINELTOOLNAME", {"note": "SENTINELARGUMENT"}, "c1"),
            tool_call("list_doctors", {}, "c2"),
        ),
        ok("SENTINELMODELTEXT"),
    )
    transport = Meta(ok_response(9))

    with caplog.at_level(logging.DEBUG):
        await pipeline.post(
            envelope(messages=[text_message(1, body="SENTINELPATIENT my knee hurts")])
        )
        outcomes = await pipeline.drain(transport, chat=chat, booking=booking)

    assert outcomes == ["replied_fallback"]
    letters = await _rows(sessionmaker_for, DeadLetterJob)
    assert len(letters) == 1
    assert letters[0].error == "agent_tool_crashed"

    sentinels = (
        "SENTINELPATIENT",
        "SENTINELARGUMENT",
        "SENTINELTOOLNAME",
        "SENTINELDOCTOR",
        "SENTINELCLINIC",
        "SENTINELSTREET",
        "SENTINELMODELTEXT",
        "SENTINELEXCEPTION",
    )

    # 1. the logs
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    for sentinel in sentinels:
        assert sentinel not in rendered, f"log: {sentinel}"

    # 2. the job results (arq stores these in Redis)
    assert all(sentinel not in "".join(outcomes) for sentinel in sentinels)

    # 3. the dead letter
    letter = json.dumps({"error": letters[0].error, "payload": letters[0].payload})
    for sentinel in sentinels:
        assert sentinel not in letter, f"dead letter: {sentinel}"

    # 4. the two agent tables
    run = (await _rows(sessionmaker_for, AgentRun))[0]
    tools = await _rows(sessionmaker_for, ToolExecution)
    tables = json.dumps(
        {
            "run": [run.outcome, run.reason, run.model, run.prompt_version, run.tenant_id],
            "tools": [[t.tool_name, list(t.argument_names), t.status, t.error_code] for t in tools],
        }
    )
    for sentinel in sentinels:
        assert sentinel not in tables, f"tables: {sentinel}"
    # The invented name became the reserved sentinel.
    assert sorted(t.tool_name for t in tools) == ["list_doctors", "unknown"]


def _completion(message: dict, finish: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1730000000,
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
        },
    )
