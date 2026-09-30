"""Acceptance: a patient books, moves and cancels on WhatsApp, safely (Task B5).

A signed webhook goes in, the queue is drained, and the whole stack runs: signature
check, dedupe, inbox, worker, tenant resolution, the booking state, the tool loop,
the in-memory Booking Service, the confirmation gate, the receipt, the reply guard,
the reservation and the Meta send.

Nothing reaches the network. The model is a scripted `FakeChatClient`, or the REAL
`OpenAIChatClient` over an `httpx2.MockTransport` where the point is the wire.

The slice's own acceptance criteria, and where each is proved:

- the full booking flow against the fake -> `test_a_patient_books_dr_karim_over_two_messages`
- SLOT_TAKEN offers alternatives and never says "confirmed" ->
  `test_a_taken_slot_is_never_confirmed_and_other_times_are_offered` and
  `test_a_model_that_says_a_taken_slot_is_confirmed_is_overruled`
- a duplicate job makes one booking -> `test_a_duplicate_job_makes_one_booking` (three
  variants)
"""

import json
import logging
import uuid

import pytest
import sqlalchemy as sa

from app.db.models import BookingAction, DeadLetterJob, Message, ToolExecution
from app.integrations.booking import ClinicInfo, Doctor, Location, PatientRef, Service
from app.integrations.booking.fake import FakeBookingClient, FakeClinic, FakeDoctor
from app.integrations.booking.memory import FailureScript, InMemoryBookingService
from app.worker.jobs.inbox import process_inbox_event
from tests.agent.test_reply_guard import claims_in
from tests.db import factories as dbf
from tests.integrations.booking_fakes import RecordingBooking, counter_ids
from tests.integrations.fakes import FakeChatClient, last_tool_result, ok, tool_call, wants_tools
from tests.whatsapp_factories import envelope, phone, text_message
from tests.worker.conftest import FROZEN_CLOCK, Meta, ok_response, wamid
from tests.worker.test_end_to_end import (  # noqa: F401 - shared harness
    OpenAI,
    completion,
    openai_settings,
    real_chat,
)

pytestmark = pytest.mark.db

WEDNESDAY_AFTERNOON = {
    "doctor_id": "doc_karim",
    "start": "2026-09-30T12:00",
    "end": "2026-09-30T17:00",
}
# The patient's WhatsApp number. It IS the patient reference the Booking Service is
# sent (V14 as the developer overrode it), which makes it the most important sentinel
# in this file.
PATIENT_PHONE = phone(1)
# A synthetic name. Hard rule 8 forbids fixtures built from real data, and this string
# must appear in the Meta request and NOWHERE else of ours.
PATIENT_NAME = "Rami Khoury"


def service(failures: FailureScript | None = None) -> InMemoryBookingService:
    """One per test: an `asyncio.Lock` binds to the loop it is first contended in."""
    return InMemoryBookingService(
        FakeBookingClient.demo(clock=FROZEN_CLOCK),
        FROZEN_CLOCK,
        id_secret=b"a fixed secret for the booking acceptance tests",
        new_id=counter_ids(),
        failures=failures,
    )


def inbound(n: int, body: str) -> dict:
    """One signed webhook body: always contact 1, so every message is the SAME patient.

    `message_payload(n)` would change the phone number, and therefore the contact and
    the conversation - and the gate would correctly find no prepared change.
    """
    return envelope(messages=[text_message(1, body=body, id=wamid(n))])


def pick(messages, start: str) -> str:
    """The `slot_id` of one time in the last search result.

    A callable script step has to do this: the id is an opaque token the service
    issued, and no test can predict it. Copying it out of the result is exactly what
    the prompt asks the model to do.
    """
    slots = last_tool_result(messages)["slots"]
    return next(slot for slot in slots if slot["start"] == start)["slot_id"]


def hold_turn(*, at: str = "2026-09-30T14:00", then: str) -> FakeChatClient:
    """Message 1: list the doctors, search Wednesday afternoon, hold `at`, ask."""
    return FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        lambda messages: wants_tools(
            tool_call("hold_appointment_slot", {"slot_id": pick(messages, at)})
        ),
        ok(then),
    )


async def sent_texts(transport: Meta) -> list[str]:
    return [json.loads(request.content)["text"]["body"] for request in transport.requests]


def model_words(text: str) -> str:
    """The model's own half of a sent reply, without our receipt line.

    `compose_reply` joins them with a blank line. The guard runs on the model's text
    BEFORE the receipt is appended, precisely so that our own ✅ or ❌ is never mistaken
    for the model's - and an acceptance test that scanned the whole sent text would be
    asserting the opposite of what the guard does.
    """
    return text.split("\n\n")[0]


async def rerun_last_event(sessionmaker, transport, **wiring):
    """Run the job for the newest `webhook_inbox` row again, as arq would.

    `pipeline.drain` POPS the queue, so a crashed job's id is gone from it - but arq
    re-runs a job whose worker died once its in-progress key expires (plan conflict
    C8). This is that re-run: the same row, a fresh `job_try`.
    """
    from tests.worker.conftest import job_context, meta_client, worker_settings

    settings = worker_settings()
    async with sessionmaker() as session:
        row_id = (
            await session.scalars(
                sa.text("SELECT id FROM webhook_inbox ORDER BY created_at DESC LIMIT 1")
            )
        ).one()
    ctx = job_context(sessionmaker, meta_client(transport, settings), settings, **wiring)
    return await process_inbox_event(ctx, str(row_id))


async def rows(sessionmaker, model, **where):
    async with sessionmaker() as session:
        statement = sa.select(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        return list((await session.scalars(statement.order_by(model.created_at))).all())


# --------------------------------------------------------------------------
# The acceptance flow
# --------------------------------------------------------------------------


async def test_a_patient_books_dr_karim_over_two_messages(sessionmaker_for, pipeline, caplog):
    """The slice's goal, end to end.

    Message 1: "Can I book Dr. Karim tomorrow at 14:00?" -> list_doctors, search,
    hold, and a reply that asks for the name and a confirmation, ending in ⏳.
    Message 2: "Yes please, <name>" -> book_appointment, and a reply ending in ✅.

    Every assertion below is about something a patient or an operator would notice:
    the two receipts, the one appointment, the one DONE row prepared by message 1 and
    decided by message 2, and two distinct 64-hex keys.
    """
    bookings = service()
    spy = RecordingBooking(bookings)
    transport = Meta(ok_response(11), ok_response(12))

    with caplog.at_level(logging.DEBUG):
        assert (
            await pipeline.post(inbound(1, "Can I book Dr. Karim tomorrow at 14:00?"))
        ).status_code == 200
        await pipeline.drain(
            transport,
            chat=hold_turn(
                then=(
                    "Wednesday 30 September at 14:00 with Dr. Karim. "
                    "What is your full name, and shall I book it?"
                )
            ),
            booking=spy,
            patient_bookings=spy,
        )
        assert (await pipeline.post(inbound(2, f"Yes please, {PATIENT_NAME}"))).status_code == 200
        await pipeline.drain(
            transport,
            chat=FakeChatClient(
                wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
                ok("Done, it's booked."),
            ),
            booking=spy,
            patient_bookings=spy,
        )

    texts = await sent_texts(transport)
    assert len(texts) == 2
    # Message 1: held, and it says so.
    assert texts[0].endswith("⏳ Dr. Karim Haddad · 2026-09-30 14:00")
    assert "✅" not in texts[0]
    assert claims_in(model_words(texts[0])) == set()
    # Message 2: booked, with the reference the service issued.
    assert "✅ Dr. Karim Haddad · 2026-09-30 14:00 · #" in texts[1]
    assert texts[1].startswith("Done, it's booked.")

    # Exactly one CONFIRMED appointment for this patient, at the service.
    held = await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))
    assert len(held) == 1
    assert held[0].status == "CONFIRMED"
    assert held[0].reference in texts[1]

    # One booking_actions row: prepared by message 1, decided by message 2.
    actions = await rows(sessionmaker_for, BookingAction)
    assert len(actions) == 1
    assert actions[0].status == "DONE"
    assert actions[0].kind == "BOOK"
    assert actions[0].appointment_id == "apt_1"
    inbounds = await rows(sessionmaker_for, Message, direction="INBOUND")
    assert actions[0].created_by_inbound_message_id == inbounds[0].id
    assert actions[0].decided_by_inbound_message_id == inbounds[1].id

    # Two distinct keys, 64 hex each, both derived from OUR inbox rows.
    keys = [key for key in spy.keys if key is not None]
    assert len(keys) == 2
    assert len(set(keys)) == 2
    assert all(len(key) == 64 and int(key, 16) >= 0 for key in keys)

    # The tool rows, in order, with argument NAMES only.
    tools = await rows(sessionmaker_for, ToolExecution)
    assert [(t.tool_name, t.status) for t in tools] == [
        ("list_doctors", "OK"),
        ("search_available_slots", "OK"),
        ("hold_appointment_slot", "OK"),
        ("book_appointment", "OK"),
    ]
    assert tools[3].argument_names == ["full_name"]
    assert await rows(sessionmaker_for, DeadLetterJob) == []

    # Nothing sensitive in the logs.
    logged = "\n".join(caplog.messages)
    for sentinel in (PATIENT_NAME, PATIENT_PHONE, "Karim Haddad", "hold_1", keys[0], keys[1]):
        assert sentinel not in logged, sentinel


async def test_cancel_takes_two_messages(sessionmaker_for, pipeline):
    """⏳❌ then ❌. A hallucinated cancellation costs a patient an appointment they
    wanted, so cancelling has the same two-message shape as booking."""
    bookings = service()
    transport = Meta(ok_response(21), ok_response(22), ok_response(23))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport,
        chat=hold_turn(then="Shall I book it?"),
        booking=bookings,
        patient_bookings=bookings,
    )
    await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
            ok("Booked."),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )
    # Message 3 asks to cancel: the first call prepares only.
    await pipeline.post(inbound(3, "actually please cancel it"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("list_my_appointments", {})),
            lambda messages: wants_tools(
                tool_call(
                    "cancel_appointment",
                    {
                        "appointment_id": last_tool_result(messages)["appointments"][0][
                            "appointment_id"
                        ]
                    },
                )
            ),
            ok("Wednesday 14:00 with Dr. Karim - shall I cancel it?"),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )

    texts = await sent_texts(transport)
    assert texts[2].endswith("⏳ ❌ Dr. Karim Haddad · 2026-09-30 14:00")
    assert claims_in(model_words(texts[2])) == set()  # nothing is cancelled yet
    # Still booked at the service.
    assert len(await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))) == 1

    # Message 4 confirms.
    transport2 = Meta(ok_response(24))
    await pipeline.post(inbound(4, "yes, cancel it"))
    await pipeline.drain(
        transport2,
        chat=FakeChatClient(
            wants_tools(tool_call("cancel_appointment", {"appointment_id": "apt_1"})),
            ok("It's cancelled."),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )

    final = (await sent_texts(transport2))[0]
    assert "❌ Dr. Karim Haddad · 2026-09-30 14:00 · #" in final
    assert await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE)) == ()
    statuses = [row.status for row in await rows(sessionmaker_for, BookingAction)]
    assert statuses.count("DONE") == 2  # the booking, and the cancellation


async def test_reschedule_takes_two_messages(sessionmaker_for, pipeline):
    """⏳ old → new, then 🔁 with the appointment's own reference kept."""
    bookings = service()
    transport = Meta(*[ok_response(30 + n) for n in range(4)])

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport,
        chat=hold_turn(then="Shall I book it?"),
        booking=bookings,
        patient_bookings=bookings,
    )
    await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
            ok("Booked."),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )
    # Message 3: hold a new time FOR the existing appointment.
    await pipeline.post(inbound(3, "can we move it to 15:40?"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("list_my_appointments", {})),
            wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
            lambda messages: wants_tools(
                tool_call(
                    "hold_appointment_slot",
                    {"slot_id": pick(messages, "2026-09-30T15:40"), "appointment_id": "apt_1"},
                )
            ),
            ok("From 14:00 to 15:40 - shall I move it?"),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )
    texts = await sent_texts(transport)
    assert texts[2].endswith(
        "⏳ Dr. Karim Haddad · 2026-09-30 14:00 → Dr. Karim Haddad · 2026-09-30 15:40"
    )
    assert claims_in(model_words(texts[2])) == set()

    transport2 = Meta(ok_response(40))
    await pipeline.post(inbound(4, "yes please"))
    await pipeline.drain(
        transport2,
        chat=FakeChatClient(
            wants_tools(tool_call("reschedule_appointment", {})),
            ok("Moved."),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )

    moved = (await sent_texts(transport2))[0]
    assert "🔁 Dr. Karim Haddad · 2026-09-30 15:40 · #" in moved
    held = await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))
    assert len(held) == 1
    assert held[0].appointment_id == "apt_1"  # same appointment, new time
    assert held[0].reference in moved


# --------------------------------------------------------------------------
# SLOT_TAKEN
# --------------------------------------------------------------------------


async def test_a_taken_slot_is_never_confirmed_and_other_times_are_offered(
    sessionmaker_for, pipeline
):
    """The slice's second acceptance criterion.

    The race, made deterministic: the script picks 14:00, and the spy's `hook` - which
    runs BEFORE the wrapped call - holds that exact slot for ANOTHER patient a moment
    before our own hold reaches the service. Our hold gets SLOT_TAKEN, the model
    searches again, and the second search does not offer 14:00.
    """
    bookings = service()
    other = PatientRef(phone(77))
    chosen: list[str] = []

    async def take_it_first() -> None:
        # Only once, and only after the script has picked a slot: this is the window
        # between our search and our hold.
        if not chosen or len(chosen) > 1:
            return
        chosen.append("taken")
        await bookings.create_hold(dbf.TENANT_A, other, chosen[0], idempotency_key="theirs")

    spy = RecordingBooking(bookings, hook=take_it_first)
    transport = Meta(ok_response(51))
    second_search: list[list[dict]] = []

    def hold_the_14(messages):
        chosen.insert(0, pick(messages, "2026-09-30T14:00"))
        return wants_tools(tool_call("hold_appointment_slot", {"slot_id": chosen[0]}))

    def offer(messages):
        second_search.append(last_tool_result(messages)["slots"])
        return ok("That time was just taken. I can offer 14:20 or 15:40 instead.")

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("list_doctors", {})),
            wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
            hold_the_14,
            wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
            offer,
        ),
        booking=spy,
        patient_bookings=spy,
    )

    text = (await sent_texts(transport))[0]
    assert claims_in(model_words(text)) == set()
    assert "✅" not in text
    # The second search did not offer the taken time.
    assert second_search
    assert all(slot["start"] != "2026-09-30T14:00" for slot in second_search[-1])
    # Nothing for our patient, and no prepared change of ours.
    assert await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE)) == ()
    assert [row.status for row in await rows(sessionmaker_for, BookingAction)] == []
    tools = await rows(sessionmaker_for, ToolExecution)
    hold = next(t for t in tools if t.tool_name == "hold_appointment_slot")
    assert (hold.status, hold.error_code) == ("ERROR", "booking_slot_taken")


async def test_a_model_that_says_a_taken_slot_is_confirmed_is_overruled(sessionmaker_for, pipeline):
    """Hard rule 5, against the worst case: the model lies.

    The same SLOT_TAKEN, and the model replies "Your appointment is confirmed!"
    anyway. The guard replaces it: Meta is sent AGENT_FALLBACK_REPLY and nothing else,
    a dead letter records that it fired, and "confirmed" appears in no sent text.
    """
    bookings = service()
    other = PatientRef(phone(77))

    async def take_it() -> None:
        slots = await bookings.search_slots(
            dbf.TENANT_A, "doc_karim", FROZEN_CLOCK(), FROZEN_CLOCK().replace(day=30, hour=20)
        )
        await bookings.create_hold(dbf.TENANT_A, other, slots[0].slot_id, idempotency_key="theirs")

    await take_it()
    transport = Meta(ok_response(61))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport,
        chat=hold_turn(then="Your appointment is confirmed!"),
        booking=bookings,
        patient_bookings=bookings,
    )

    texts = await sent_texts(transport)
    assert len(texts) == 1
    assert texts[0] == pipeline.settings.agent_fallback_reply
    assert "confirmed" not in texts[0].lower()
    assert claims_in(model_words(texts[0])) == set()
    letters = {row.error for row in await rows(sessionmaker_for, DeadLetterJob)}
    assert "agent_unconfirmed_claim" in letters
    assert await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE)) == ()


# --------------------------------------------------------------------------
# Duplicates: one booking, three ways
# --------------------------------------------------------------------------


async def test_a_duplicate_job_makes_one_booking_after_a_crash(sessionmaker_for, pipeline):
    """Variant 1: a worker dies between the booking call and T1b (plan conflict C8).

    arq re-runs the job. The spy sees TWO `create_appointment` calls with the SAME key,
    and the service holds ONE appointment - which is the whole reason hard rule 6
    exists.
    """
    bookings = service()
    spy = RecordingBooking(bookings)
    transport = Meta(ok_response(71), ok_response(72))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport, chat=hold_turn(then="Shall I book it?"), booking=spy, patient_bookings=spy
    )

    class DyingWorker(Exception):
        """A plain Exception subclass: nothing in the job catches it, so the job
        escapes before T1b exactly as a killed worker's would."""

    def die(messages):
        raise DyingWorker

    await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
    with pytest.raises(DyingWorker):
        await pipeline.drain(
            transport,
            chat=FakeChatClient(
                wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
                die,
            ),
            booking=spy,
            patient_bookings=spy,
        )

    # The booking happened; nothing of ours recorded it.
    assert len(await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))) == 1
    assert [row.status for row in await rows(sessionmaker_for, BookingAction)] == ["PENDING"]

    # Expire the lease, the way a dead worker's in-progress key expires, and re-run
    # the SAME row - `drain` has already popped its id from the queue.
    async with sessionmaker_for() as session:
        await session.execute(
            sa.text("UPDATE webhook_inbox SET locked_until = now() - interval '1 second'")
        )
        await session.commit()
    await rerun_last_event(
        sessionmaker_for,
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
            ok("Done, it's booked."),
        ),
        booking=spy,
        patient_bookings=spy,
        job_try=2,
    )

    booked = [call for call in spy.calls if call.method == "create_appointment"]
    assert len(booked) == 2
    assert booked[0].kwargs["idempotency_key"] == booked[1].kwargs["idempotency_key"]
    held = await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))
    assert len(held) == 1
    texts = await sent_texts(transport)
    assert held[0].reference in texts[-1]
    assert [row.status for row in await rows(sessionmaker_for, BookingAction)] == ["DONE"]


async def test_a_rerun_that_spells_the_name_differently_still_makes_one_booking(
    sessionmaker_for, pipeline
):
    """Variant 2: the key CHANGES, and V13 saves it anyway.

    A re-run is a fresh model run, and it may spell the name differently - a different
    key, so the replay store cannot help. Natural idempotency per target does: booking
    a hold this patient already converted returns that appointment.
    """
    bookings = service()
    spy = RecordingBooking(bookings)
    transport = Meta(ok_response(81), ok_response(82))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport, chat=hold_turn(then="Shall I book it?"), booking=spy, patient_bookings=spy
    )

    class DyingWorker(Exception):
        pass

    await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
    with pytest.raises(DyingWorker):
        await pipeline.drain(
            transport,
            chat=FakeChatClient(
                wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
                lambda messages: (_ for _ in ()).throw(DyingWorker()),
            ),
            booking=spy,
            patient_bookings=spy,
        )

    async with sessionmaker_for() as session:
        await session.execute(
            sa.text("UPDATE webhook_inbox SET locked_until = now() - interval '1 second'")
        )
        await session.commit()
    # A DIFFERENT spelling: "Rami  Khoury" normalises to the same thing, so use a
    # genuinely different name, as a second model run really might.
    await rerun_last_event(
        sessionmaker_for,
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": "Rami Khouri"})),
            ok("Done, it's booked."),
        ),
        booking=spy,
        patient_bookings=spy,
        job_try=2,
    )

    booked = [call for call in spy.calls if call.method == "create_appointment"]
    assert len(booked) == 2
    assert booked[0].kwargs["idempotency_key"] != booked[1].kwargs["idempotency_key"]
    assert len(await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))) == 1


async def test_the_same_webhook_delivered_twice_makes_one_booking(sessionmaker_for, pipeline):
    """Variant 3: Meta's own duplicate. Hard rule 2 dedupes to one inbox row, so there
    is one job, one turn and one booking - and the key never even has to work."""
    bookings = service()
    spy = RecordingBooking(bookings)
    transport = Meta(ok_response(91), ok_response(92))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport, chat=hold_turn(then="Shall I book it?"), booking=spy, patient_bookings=spy
    )

    body = inbound(2, f"yes, {PATIENT_NAME}")
    assert (await pipeline.post(body)).status_code == 200
    assert (await pipeline.post(body)).status_code == 200
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
            ok("Done, it's booked."),
        ),
        booking=spy,
        patient_bookings=spy,
    )

    inbounds = await rows(sessionmaker_for, Message, direction="INBOUND")
    assert len(inbounds) == 2  # the two distinct messages, not three
    booked = [call for call in spy.calls if call.method == "create_appointment"]
    assert len(booked) == 1
    assert len(await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))) == 1


# --------------------------------------------------------------------------
# V6, the wire, and the sentinel sweep
# --------------------------------------------------------------------------


async def test_an_unknown_outcome_is_never_reported_as_booked_or_failed(sessionmaker_for, pipeline):
    """V6, end to end. `UNKNOWN_AFTER`: the service DID book it, and the answer was
    lost.

    The sent text claims nothing either way, there is no receipt, a dead letter carries
    the key, and a later confirmation gets that same booking back through V13.
    """
    failures = FailureScript()
    bookings = service(failures)
    transport = Meta(ok_response(101), ok_response(102))

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    await pipeline.drain(
        transport,
        chat=hold_turn(then="Shall I book it?"),
        booking=bookings,
        patient_bookings=bookings,
    )
    failures.push("create_appointment", "UNKNOWN_AFTER")
    await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
    await pipeline.drain(
        transport,
        chat=FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
            ok("The clinic team will check and get back to you."),
        ),
        booking=bookings,
        patient_bookings=bookings,
    )

    text = (await sent_texts(transport))[-1]
    assert claims_in(model_words(text)) == set()
    for symbol in ("✅", "🔁", "❌"):
        assert symbol not in text
    action = (await rows(sessionmaker_for, BookingAction))[-1]
    assert action.status == "UNCERTAIN"
    letter = next(
        row
        for row in await rows(sessionmaker_for, DeadLetterJob)
        if row.error == "booking_uncertain"
    )
    assert letter.payload["booking"]["idempotency_key"] == action.last_idempotency_key
    # The service really did book it, and V13 returns that booking to a later call.
    held = await bookings.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))
    assert len(held) == 1


async def test_nothing_sensitive_reaches_logs_job_results_dead_letters_or_the_tables(
    sessionmaker_for, pipeline, caplog
):
    """Hard rule 8, swept over one whole booking.

    Six sentinels, each for its own reason: the patient's words, their NAME (a tool
    argument), a DOCTOR's name and an appointment time (content, which belongs only in
    `messages.text`), the patient's phone number (the patient reference now), and the
    Booking Service's hold id and our key (identifiers).

    A custom clinic, so the doctor's name is a sentinel the demo data does not contain.
    """
    said = "SENTINEL-patient-words"
    doctor_name = "Dr. SENTINELDOCTOR"
    clinic = FakeClinic(
        info=ClinicInfo(
            name="SENTINELCLINIC",
            timezone="Asia/Beirut",
            locations=(Location(name="x", address="y"),),
        ),
        doctors=(
            FakeDoctor(
                doctor=Doctor(
                    doctor_id="doc_sentinel",
                    name=doctor_name,
                    specialty="General practice",
                    services=(Service(service_id="svc", name="Consultation", duration_minutes=20),),
                ),
                starts={2: (__import__("datetime").time(14, 0),)},
                slot_minutes=20,
            ),
        ),
    )
    bookings = InMemoryBookingService(
        FakeBookingClient(clinics={dbf.TENANT_A: clinic}, clock=FROZEN_CLOCK),
        FROZEN_CLOCK,
        id_secret=b"sentinel-secret",
        new_id=counter_ids(),
    )
    transport = Meta(ok_response(111), ok_response(112))

    with caplog.at_level(logging.DEBUG):
        await pipeline.post(inbound(1, said))
        await pipeline.drain(
            transport,
            chat=FakeChatClient(
                wants_tools(tool_call("list_doctors", {})),
                wants_tools(
                    tool_call(
                        "search_available_slots",
                        {**WEDNESDAY_AFTERNOON, "doctor_id": "doc_sentinel"},
                    )
                ),
                lambda messages: wants_tools(
                    tool_call(
                        "hold_appointment_slot", {"slot_id": pick(messages, "2026-09-30T14:00")}
                    )
                ),
                ok("Shall I book it?"),
            ),
            booking=bookings,
            patient_bookings=bookings,
        )
        await pipeline.post(inbound(2, f"yes, {PATIENT_NAME}"))
        await pipeline.drain(
            transport,
            chat=FakeChatClient(
                wants_tools(tool_call("book_appointment", {"full_name": PATIENT_NAME})),
                ok("Booked."),
            ),
            booking=bookings,
            patient_bookings=bookings,
        )

    action = (await rows(sessionmaker_for, BookingAction))[-1]
    sentinels = (
        said,
        PATIENT_NAME,
        PATIENT_PHONE,
        "SENTINELDOCTOR",
        "2026-09-30 14:00",
        action.hold_id or "hold_1",
        action.last_idempotency_key,
    )

    # 1. Log lines.
    logged = "\n".join(caplog.messages)
    for sentinel in sentinels:
        assert sentinel not in logged, f"log: {sentinel}"

    # 2. The agent tables and booking_actions: codes, counts and ids only.
    async with sessionmaker_for() as session:
        for table in ("agent_runs", "tool_executions", "booking_actions"):
            dumped = str(
                (await session.execute(sa.text(f"SELECT * FROM {table}"))).mappings().all()
            )
            for sentinel in (
                said,
                PATIENT_NAME,
                PATIENT_PHONE,
                "SENTINELDOCTOR",
                "2026-09-30 14:00",
            ):
                assert sentinel not in dumped, f"{table}: {sentinel}"
            # booking_actions DOES hold the key - that is deliberate (V6).
            if table != "booking_actions":
                assert action.last_idempotency_key not in dumped, f"{table}: the key"

    # 3. Dead letters: none here. And what reaches Redis is OUR row id and nothing
    # else - the queue is handed a uuid, never a payload, a wamid or a phone number
    # (VS-004's plan note C2, still true now that a turn can book).
    assert await rows(sessionmaker_for, DeadLetterJob) == []
    enqueued = pipeline.queue.enqueued_ever
    assert enqueued
    assert all(isinstance(row_id, uuid.UUID) for row_id in enqueued)
    for sentinel in sentinels:
        assert sentinel not in str(enqueued), f"queue: {sentinel}"

    # 4. The receipt IS content, and it lives in messages.text - the one place it may.
    replies = await rows(sessionmaker_for, Message, direction="OUTBOUND")
    assert "SENTINELDOCTOR" in replies[-1].text
    assert "2026-09-30 14:00" in replies[-1].text
    # But the patient's name never appears in a reply either: the model was not told it.
    assert PATIENT_NAME not in replies[-1].text


async def test_the_booking_turn_on_the_wire_never_sends_identity_or_keys(pipeline):
    """The REAL `OpenAIChatClient`, over an `httpx2.MockTransport`.

    Every request carries the eight tools in the registry's order, and no request body
    contains the tenant, the phone number, a wamid, a hold id or a key. This is the one
    test that reads what would actually leave the process.
    """
    import httpx2

    bookings = service()

    def tool_response(name: str, arguments: str, call_id: str) -> httpx2.Response:
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
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
            },
        )

    class Wire(OpenAI):
        """Reads the slot_id out of the previous tool message, as a model would."""

        async def __call__(self, request):
            self.requests.append(request)
            body = json.loads(request.content)
            calls = len(self.requests)
            if calls == 1:
                return tool_response("list_doctors", "{}", "call_1")
            if calls == 2:
                return tool_response(
                    "search_available_slots", json.dumps(WEDNESDAY_AFTERNOON), "call_2"
                )
            if calls == 3:
                last = json.loads(body["messages"][-1]["content"])
                slot_id = next(
                    s["slot_id"] for s in last["slots"] if s["start"] == "2026-09-30T14:00"
                )
                return tool_response(
                    "hold_appointment_slot", json.dumps({"slot_id": slot_id}), "call_3"
                )
            return completion("Shall I book it?")

    settings = openai_settings()
    wire = Wire()
    chat = real_chat(wire, settings)

    await pipeline.post(inbound(1, "book me at 14:00 tomorrow"))
    outcomes = await pipeline.drain(
        Meta(ok_response(121)),
        chat=chat,
        booking=bookings,
        patient_bookings=bookings,
        settings=settings,
    )

    assert outcomes == ["replied"]
    bodies = wire.bodies()
    assert len(bodies) == 4
    for body in bodies:
        assert [tool["function"]["name"] for tool in body["tools"]] == [
            "get_clinic_information",
            "list_doctors",
            "search_available_slots",
            "list_my_appointments",
            "hold_appointment_slot",
            "book_appointment",
            "reschedule_appointment",
            "cancel_appointment",
        ]
    rendered = json.dumps(bodies)
    for sentinel in (dbf.TENANT_A, PATIENT_PHONE, "wamid.", "hold_1", "idempotency"):
        assert sentinel not in rendered, sentinel
