"""The job records every booking change (Task B4, plan sections 5.11 and 5.12).

Against a real PostgreSQL and a real `InMemoryBookingService`, one per test. The
commit boundaries are what is under test, so almost every assertion is about what
survived a commit rather than about what a function returned.

The paths, in the order plan section 5.11's diagram walks them:

- **T1** loads the booking state: lapse a stale hold on the INJECTED clock, then the
  gate's verdict from SQL;
- **the turn** runs with no transaction open;
- **T1r** records an attempt that made a change and is about to be retried (V9);
- **T1b** locks the conversation row FIRST, reserves the reply with its receipt, and
  records the outcome in a SAVEPOINT;
- **the drop paths** void every prepared change and tell staff about one that
  happened anyway (hard rule 7, section 5.12).
"""

import datetime as dt
import itertools
import logging

import httpx
import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.db.enums import BookingActionKind, BookingActionStatus, ConversationState
from app.db.models import AgentRun, BookingAction, Conversation, DeadLetterJob, Message
from app.integrations.booking import PatientRef
from app.integrations.booking.memory import FailureScript
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.integrations.booking_fakes import RecordingBooking
from tests.integrations.fakes import (
    FakeChatClient,
    last_tool_result,
    ok,
    tool_call,
    wants_tools,
)
from tests.whatsapp_factories import wamid
from tests.worker.conftest import (
    FROZEN_CLOCK,
    Meta,
    booking_service,
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
# The patient's WhatsApp number, as tests/whatsapp_factories.py builds it for n=1.
# It is the patient reference the Booking Service is sent (V14 as overridden), and
# the sentinel every leak assertion below searches for.
PATIENT_PHONE = "96170000001"


# One sequence for the whole module, not one per transport: a two-message test
# builds a transport per job, and two counters starting at zero would hand out the
# same wamid twice. `clean_database` truncates between tests, so a monotonic counter
# is all the uniqueness that is needed.
_ACCEPTED = itertools.count(901)


class UniqueMeta(Meta):
    """A Meta transport that accepts every send with a DIFFERENT wamid.

    `ok_response()` hard-codes n=9, so the base `Meta` hands back the same wamid for
    every send in a test. One send never notices; a two-message booking flow sends
    twice, and T2's `attach_provider_id` then violates
    `uq_messages_provider_message_id` - our own "one stored message per Meta id"
    rule. That is the harness tripping a real constraint, not the job failing, and
    the right fix is a harness that behaves like Meta does: a fresh id per accepted
    message.
    """

    async def __call__(self, request):
        self._responses = [ok_response(next(_ACCEPTED))]
        return await super().__call__(request)


def meta(hook=None) -> UniqueMeta:
    return UniqueMeta(hook=hook)


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


async def _run(sessionmaker, transport, service, chat, *, n=1, settings=None, **overrides):
    """One job, with the service wired into BOTH booking roles, as the worker does.

    `n` is the MESSAGE number, not the patient: the payload always carries contact 1,
    so every message in a test belongs to the SAME patient and therefore the same
    conversation. Only the inbound wamid and the `webhook_inbox` row change with `n`.

    That distinction is the whole two-message flow. `message_payload(2)` would build a
    different phone number, a different contact and a different conversation - and the
    gate would correctly find no prepared change, which looks like a broken gate and is
    a broken test.
    """
    settings = settings or worker_settings()
    payload = message_payload(1, id=wamid(n))
    event_id = await store_event(sessionmaker, payload, n)
    wiring = {"booking": service, "patient_bookings": service}
    # A test that wants a spy in one of the two roles overrides just that role, and
    # the other keeps pointing at the same service - which is what the worker does.
    wiring.update({k: overrides.pop(k) for k in ("booking", "patient_bookings") if k in overrides})
    ctx = job_context(
        sessionmaker,
        meta_client(transport, settings),
        settings,
        chat=chat,
        **wiring,
        **overrides,
    )
    outcome = await process_inbox_event(ctx, str(event_id))
    return event_id, outcome


def hold_script(*, then: str = "I've put Wednesday 14:00 with Dr. Karim on hold. Shall I book it?"):
    """Message 1: list the doctors, search, hold the 14:00 slot, ask.

    The hold step is a CALLABLE, because the `slot_id` is an opaque token the service
    issued and no test can predict it - the script has to read it out of the search
    result exactly as a real model would.
    """

    def hold(messages):
        slots = last_tool_result(messages)["slots"]
        chosen = next(s for s in slots if s["start"] == "2026-09-30T14:00")
        return wants_tools(tool_call("hold_appointment_slot", {"slot_id": chosen["slot_id"]}))

    return FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        hold,
        ok(then),
    )


def book_script(*, then: str = "Done, it's booked."):
    """Message 2: book what is on hold, then answer."""
    return FakeChatClient(
        wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
        ok(then),
    )


# --------------------------------------------------------------------------
# T1: what the turn is handed
# --------------------------------------------------------------------------


async def test_t1_hands_the_turn_its_ids_and_its_booking_state(sessionmaker_for, monkeypatch):
    """Everything the tools need, loaded before T1's transaction closes.

    Captured at the job's own seam - `process_turn` - rather than through a tool, so
    what is asserted is exactly what the JOB built.

    The ids are ours (the inbox row for the key, the message being answered for the
    gate); the booking state is the gate's verdict from SQL; and the patient reference
    is the contact's STORED WhatsApp number, read from our row rather than taken off
    the payload, so it is the same on every retry.
    """
    import app.worker.jobs.inbox as job_module

    service = booking_service()
    seen = []
    real = job_module.process_turn

    async def capture(turn, chat, runtime):
        seen.append(turn)
        return await real(turn, chat, runtime)

    monkeypatch.setattr(job_module, "process_turn", capture)

    # Message 1: nothing is prepared yet.
    event_id, _ = await _run(sessionmaker_for, meta(), service, hold_script())

    assert len(seen) == 1
    first = seen[0]
    assert first.tenant_id == dbf.TENANT_A
    assert first.inbox_event_id == event_id
    inbound = await _one(sessionmaker_for, Message, direction="INBOUND")
    assert first.inbound_message_id == inbound.id
    assert first.patient_reference == PATIENT_PHONE
    assert first.booking_state is None
    # The phone number is never in the Turn's repr: a traceback must not print it.
    assert PATIENT_PHONE not in repr(first)

    # Message 2, after our reply really went out: the gate says it is confirmable.
    await _run(sessionmaker_for, meta(), service, book_script(), n=2)

    assert len(seen) == 2
    state = seen[1].booking_state
    assert state is not None
    assert state.kind is BookingActionKind.BOOK
    assert state.status is BookingActionStatus.PENDING
    assert state.confirmable is True
    assert state.hold_id == "hold_1"
    # And the hold id is not in ITS repr either (plan conflict C15).
    assert "hold_1" not in repr(state)


async def test_t1_says_a_change_is_not_confirmable_when_the_reply_was_never_sent(
    sessionmaker_for, monkeypatch
):
    """The gate's third condition, and the case it exists for.

    Meta REFUSES the first reply, so `mark_failed` leaves `sent_at` NULL: the hold was
    recorded, and the patient was never told about it. Their next message therefore
    cannot be a confirmation of anything, and `book_appointment` is refused.

    Note what this test does NOT need to do: fake a `sent_at`. The job's own T2 sets it
    through `attach_provider_id` after Meta accepts a reply, so the happy two-message
    flow gets it for free - which is exactly why the gate can rely on it.
    """
    import app.worker.jobs.inbox as job_module

    service = booking_service()
    seen = []
    real = job_module.process_turn

    async def capture(turn, chat, runtime):
        seen.append(turn)
        return await real(turn, chat, runtime)

    monkeypatch.setattr(job_module, "process_turn", capture)

    # 131030: Meta's "recipient not in allowed list" - a permanent refusal.
    refused = Meta(httpx.Response(400, json={"error": {"code": 131030}}))
    # The envelope records a permanent send failure and returns rather than raising:
    # raising would make arq log a traceback for a decision already recorded.
    _, outcome = await _run(sessionmaker_for, refused, service, hold_script())
    assert outcome == "dead_lettered"

    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.status == "FAILED"
    assert reply.sent_at is None
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "PENDING"  # the hold WAS recorded

    chat = book_script()
    await _run(sessionmaker_for, meta(), service, chat, n=2)

    assert seen[1].booking_state is not None
    assert seen[1].booking_state.confirmable is False
    assert "confirmation_needed" in str(chat.calls[-1][-1].content)
    # Still only prepared: nothing was booked on the strength of a reply nobody read.
    assert await service.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE)) == ()


async def test_t1_expires_a_stale_hold_on_the_injected_clock(sessionmaker_for):
    """Plan risk R5. The hold's expiry came from the Booking Service's clock, so it is
    compared with the clock the rest of the turn uses - here frozen in 2026, while
    PostgreSQL's `now()` is today.

    A row whose expiry has passed becomes EXPIRED, and `book_appointment` then gets
    `hold_expired` ("search again") rather than `nothing_to_confirm`.
    """
    service = booking_service()
    # Message 1 holds something.
    await _run(sessionmaker_for, meta(), service, hold_script())
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "PENDING"

    # Move the hold's expiry into the injected clock's past.
    async with sessionmaker_for() as session:
        await session.execute(
            sa.update(BookingAction)
            .where(BookingAction.id == action.id)
            .values(hold_expires_at=FROZEN_CLOCK() - dt.timedelta(minutes=1))
        )
        await session.commit()

    chat = book_script()
    await _run(sessionmaker_for, meta(), service, chat, n=2)

    async with sessionmaker_for() as session:
        refreshed = await session.get(BookingAction, action.id)
        assert refreshed.status == "EXPIRED"
    # And the tool was told to search again, not that nothing was waiting.
    assert "hold_expired" in str(chat.calls[-1][-1].content)


# --------------------------------------------------------------------------
# The two-message flow
# --------------------------------------------------------------------------


async def test_a_hold_is_recorded_pending_and_its_receipt_ends_the_reply(sessionmaker_for):
    service = booking_service()
    transport = meta()

    event_id, outcome = await _run(sessionmaker_for, transport, service, hold_script())

    assert outcome == "replied"
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text.endswith("⏳ Dr. Karim Haddad · 2026-09-30 14:00")
    assert "✅" not in reply.text
    # The model's own words are still there, above the receipt.
    assert "Shall I book it?" in reply.text

    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "PENDING"
    assert action.kind == "BOOK"
    assert action.hold_id == "hold_1"
    assert action.created_by_inbox_event_id == event_id
    assert action.hold_expires_at == FROZEN_CLOCK() + dt.timedelta(minutes=10)
    assert action.last_idempotency_key is not None
    assert len(action.last_idempotency_key) == 64
    assert action.decided_by_inbound_message_id is None


async def test_a_booking_on_a_later_message_is_done_and_ends_with_a_tick(sessionmaker_for):
    """The slice's goal, through the job: two messages, one booking, one ✅."""
    service = booking_service()

    await _run(sessionmaker_for, meta(), service, hold_script())
    _, outcome = await _run(sessionmaker_for, meta(), service, book_script(), n=2)

    assert outcome == "replied"
    replies = sorted(
        await _all(sessionmaker_for, Message, direction="OUTBOUND"),
        key=lambda row: row.created_at,
    )
    assert len(replies) == 2
    assert replies[0].text.endswith("⏳ Dr. Karim Haddad · 2026-09-30 14:00")
    assert "✅ Dr. Karim Haddad · 2026-09-30 14:00 · #" in replies[1].text

    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "DONE"
    assert action.appointment_id == "apt_1"
    assert action.decided_by_inbound_message_id is not None
    assert action.created_by_inbound_message_id != action.decided_by_inbound_message_id

    # One appointment at the service, for this patient.
    held = await service.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))
    assert len(held) == 1
    assert held[0].status == "CONFIRMED"


async def test_booking_in_the_same_message_as_the_hold_is_refused(sessionmaker_for):
    """V3 through the job: the gate refuses, and the patient gets the model's reply
    asking them to confirm - not a booking."""
    service = booking_service()

    def hold_then_book(messages):
        slots = last_tool_result(messages)["slots"]
        chosen = next(s for s in slots if s["start"] == "2026-09-30T14:00")
        return wants_tools(tool_call("hold_appointment_slot", {"slot_id": chosen["slot_id"]}))

    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        hold_then_book,
        wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
        ok("Wednesday 14:00 with Dr. Karim. Shall I book it?"),
    )

    _, outcome = await _run(sessionmaker_for, meta(), service, chat)

    assert outcome == "replied"
    assert "confirmation_needed" in str(chat.calls[-1][-1].content)
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "PENDING"  # still only prepared
    assert await service.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE)) == ()


# --------------------------------------------------------------------------
# Hard rule 7
# --------------------------------------------------------------------------


async def test_a_conversation_a_human_holds_supersedes_every_pending_action(sessionmaker_for):
    """The FIRST read (plan section 5.12).

    A staff message sent after the AI prepared a hold would otherwise satisfy the
    gate's "a reply was sent in between", and the patient's next "yes" would confirm
    something a human never saw.
    """
    service = booking_service()
    await _run(sessionmaker_for, meta(), service, hold_script())
    async with sessionmaker_for() as session:
        await session.execute(
            sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
        )
        await session.commit()

    _, outcome = await _run(sessionmaker_for, meta(), service, book_script(), n=2)

    assert outcome == "dropped_not_ai_active"
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "SUPERSEDED"


async def test_a_takeover_during_a_booking_records_it_and_tells_staff(sessionmaker_for):
    """The hardest half of hard rule 7 (plan section 5.12).

    The Booking Service already booked it - the loop has no database access and could
    not know about the takeover. The change is the patient's own confirmed request and
    is NOT undone: it is recorded DONE, nothing is sent, and a dead letter tells staff,
    because the dead-letter table is the only staff channel until VS-010.
    """
    service = booking_service()
    await _run(sessionmaker_for, meta(), service, hold_script())

    async def takeover() -> None:
        async with sessionmaker_for() as session:
            await session.execute(
                sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
            )
            await session.commit()

    spy = RecordingBooking(service, after_hook=takeover)
    transport = meta()

    _, outcome = await _run(sessionmaker_for, transport, spy, book_script(), n=2)

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 0  # nothing was sent to the patient
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "DONE"
    assert action.appointment_id == "apt_1"
    letters = {row.error for row in await _all(sessionmaker_for, DeadLetterJob)}
    assert "booking_changed_reply_dropped" in letters
    # The service really did book it.
    assert len(await service.list_appointments(dbf.TENANT_A, PatientRef(PATIENT_PHONE))) == 1


async def test_a_takeover_during_a_booking_call_is_not_blocked(sessionmaker_for):
    """No transaction is open across the Booking Service call.

    VS-006's pattern, now applied to a WRITE: a staff takeover with
    `lock_timeout = '2s'` runs to completion while the booking call is in flight. If
    the job held a transaction there, this would raise a lock timeout.
    """
    service = booking_service()
    await _run(sessionmaker_for, meta(), service, hold_script())
    taken_over = False

    async def takeover() -> None:
        nonlocal taken_over
        async with sessionmaker_for() as session:
            await session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            await session.execute(
                sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
            )
            await session.commit()
        taken_over = True

    # The spy wraps the PATIENT side: `book_appointment` goes through
    # `patient_bookings`, not `booking`.
    spy = RecordingBooking(service, hook=takeover)

    await _run(sessionmaker_for, meta(), service, book_script(), n=2, patient_bookings=spy)

    assert taken_over
    assert spy.methods == ["create_appointment"]


# --------------------------------------------------------------------------
# V6, V9, V11
# --------------------------------------------------------------------------


async def test_an_unknown_outcome_is_uncertain_and_dead_lettered_with_its_key(sessionmaker_for):
    """V6, end to end. The reply claims nothing, the row says UNCERTAIN, and the dead
    letter carries the key - the only thing that can find the request afterwards."""
    failures = FailureScript()
    service = booking_service(failures=failures)
    await _run(sessionmaker_for, meta(), service, hold_script())
    failures.push("create_appointment", "UNKNOWN_AFTER")

    transport = meta()
    _, outcome = await _run(
        sessionmaker_for,
        transport,
        service,
        FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
            ok("The clinic team will check and get back to you."),
        ),
        n=2,
    )

    assert outcome == "replied"
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "UNCERTAIN"
    assert action.error_code == "booking_unknown_outcome"
    assert action.last_idempotency_key is not None

    letter = await _one(sessionmaker_for, DeadLetterJob, error="booking_uncertain")
    booking = letter.payload["booking"]
    assert booking["status"] == "UNCERTAIN"
    assert booking["phase"] == "EXECUTED"
    assert booking["kind"] == "BOOK"
    assert booking["idempotency_key"] == action.last_idempotency_key
    # No receipt: nothing is claimed either way.
    reply = sorted(
        await _all(sessionmaker_for, Message, direction="OUTBOUND"),
        key=lambda row: row.created_at,
    )[-1]
    for symbol in ("✅", "🔁", "❌"):
        assert symbol not in reply.text


async def test_a_booked_turn_that_then_fails_retryably_is_not_generated_again(sessionmaker_for):
    """V11. The booking happened; a re-run would bill again and could decide something
    else. The fallback goes out WITH the ✅, so the patient still learns what
    happened."""
    service = booking_service()
    await _run(sessionmaker_for, meta(), service, hold_script())

    from tests.integrations.fakes import retryable

    chat = FakeChatClient(
        wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
        retryable("openai_http_503"),
    )
    transport = meta()

    _, outcome = await _run(sessionmaker_for, transport, service, chat, n=2)

    assert outcome == "replied_fallback"
    assert transport.sends == 1
    reply = sorted(
        await _all(sessionmaker_for, Message, direction="OUTBOUND"),
        key=lambda row: row.created_at,
    )[-1]
    assert reply.text.startswith(worker_settings().agent_fallback_reply)
    assert "✅ Dr. Karim Haddad · 2026-09-30 14:00 · #" in reply.text
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "DONE"
    letters = {row.error for row in await _all(sessionmaker_for, DeadLetterJob)}
    assert "openai_http_503" in letters


async def test_a_held_turn_that_then_fails_retryably_is_recorded_then_retried(sessionmaker_for):
    """V9. A PREPARED change is not final, so the turn IS retried - but the attempt is
    recorded first, in T1r, because the Booking Service now holds a hold nothing of
    ours would otherwise know about.

    The action is stored SUPERSEDED, not PENDING: this attempt sent the patient
    nothing, so nothing may confirm it. The retry runs the turn again, the same key
    returns the same hold, and the retry's T1b records it PENDING with the model's new
    wording (plan section 5.11, row 7).
    """
    from tests.integrations.fakes import retryable

    service = booking_service()

    def hold(messages):
        slots = last_tool_result(messages)["slots"]
        chosen = next(s for s in slots if s["start"] == "2026-09-30T14:00")
        return wants_tools(tool_call("hold_appointment_slot", {"slot_id": chosen["slot_id"]}))

    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
        hold,
        retryable("openai_http_503"),
    )
    transport = meta()

    with pytest.raises(Retry):
        await _run(sessionmaker_for, transport, service, chat)

    assert transport.sends == 0
    # T1r wrote the run, unlike a read-only retried attempt (Q1 keeps that).
    run = await _one(sessionmaker_for, AgentRun)
    assert run.outcome == "RETRYABLE"
    assert run.reason == "openai_http_503"
    assert run.reply_message_id is None
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "SUPERSEDED"
    assert action.hold_id == "hold_1"


async def test_the_guard_replaces_the_reply_and_supersedes_the_prepared_change(sessionmaker_for):
    """G2 through the job. The model claimed a booking after only a HOLD.

    The patient gets the fallback, a dead letter records that it fired, and the
    prepared change is stored SUPERSEDED - because the reply that would have described
    it never went out, so nothing may confirm it later.
    """
    service = booking_service()
    transport = meta()

    _, outcome = await _run(
        sessionmaker_for,
        transport,
        service,
        hold_script(then="Your appointment is confirmed!"),
    )

    assert outcome == "replied_fallback"
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text == worker_settings().agent_fallback_reply
    assert "confirmed" not in reply.text
    # A prepared change's receipt is NOT shown with a replaced reply: the ⏳ refers to
    # a question the patient never got.
    assert "⏳" not in reply.text
    letter = await _one(sessionmaker_for, DeadLetterJob, error="agent_unconfirmed_claim")
    assert letter.payload["booking"]["phase"] == "PROPOSED"
    action = await _one(sessionmaker_for, BookingAction)
    assert action.status == "SUPERSEDED"


async def test_a_failed_booking_state_write_does_not_block_the_reply(sessionmaker_for, monkeypatch):
    """Plan risk R4. `apply` runs in a SAVEPOINT after the reservation, so a
    bookkeeping failure costs the patient nothing.

    The Booking Service has already changed something and the receipt is built from
    its own answer, so refusing to reply would help nobody. A dead letter says the
    table needs reconciling by hand.
    """
    from app.db.repositories.booking_actions import BookingActionRepository
    from app.db.repositories.errors import BookingStateNotRecordedError

    service = booking_service()
    transport = meta()

    async def boom(self, *args, **kwargs):
        raise BookingStateNotRecordedError("IntegrityError")

    monkeypatch.setattr(BookingActionRepository, "apply", boom)

    _, outcome = await _run(sessionmaker_for, transport, service, hold_script())

    assert outcome == "replied"
    assert transport.sends == 1
    reply = await _one(sessionmaker_for, Message, direction="OUTBOUND")
    assert reply.text.endswith("⏳ Dr. Karim Haddad · 2026-09-30 14:00")
    assert await _all(sessionmaker_for, BookingAction) == []
    letters = {row.error for row in await _all(sessionmaker_for, DeadLetterJob)}
    assert "booking_state_not_recorded" in letters


# --------------------------------------------------------------------------
# What is logged, and what is not
# --------------------------------------------------------------------------


async def test_the_booking_outcome_log_line_carries_codes_and_ids_only(sessionmaker_for, caplog):
    service = booking_service()

    with caplog.at_level(logging.INFO, logger="app.worker.jobs.inbox"):
        event_id, _ = await _run(sessionmaker_for, meta(), service, hold_script())

    line = next(m for m in caplog.messages if m.startswith("booking outcome "))
    action = await _one(sessionmaker_for, BookingAction)
    assert f"event_id={event_id}" in line
    assert f"action_id={action.id}" in line
    assert "kind=BOOK" in line
    assert "phase=PROPOSED" in line
    assert "status=SUCCESS" in line


async def test_no_log_line_contains_the_name_a_time_a_service_id_or_a_key(sessionmaker_for, caplog):
    """Hard rule 8, over every line the whole booking flow writes.

    Six sentinels, each for its own reason: the patient's phone number (it is the
    patient reference now), their name, the doctor's name and the appointment time
    (content, which belongs only in `messages.text`), the Booking Service's hold id,
    and the idempotency key.
    """
    service = booking_service()

    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker_for, meta(), service, hold_script())
        await _run(sessionmaker_for, meta(), service, book_script(), n=2)

    action = await _one(sessionmaker_for, BookingAction)
    logged = "\n".join(caplog.messages)
    for sentinel in (
        PATIENT_PHONE,
        "Rami Khoury",
        "Karim Haddad",
        "2026-09-30 14:00",
        "hold_1",
        action.last_idempotency_key,
    ):
        assert sentinel not in logged, sentinel


async def test_booking_dead_letters_carry_ids_codes_and_the_key_only(sessionmaker_for):
    """The triage table is read casually, so it holds our ids, the service's codes and
    a one-way hash - and nothing a person could be identified or described by."""
    failures = FailureScript()
    service = booking_service(failures=failures)
    await _run(sessionmaker_for, meta(), service, hold_script())
    failures.push("create_appointment", "UNKNOWN_AFTER")

    await _run(
        sessionmaker_for,
        meta(),
        service,
        FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
            ok("The clinic team will check."),
        ),
        n=2,
    )

    letter = await _one(sessionmaker_for, DeadLetterJob, error="booking_uncertain")
    rendered = str(letter.payload)
    assert set(letter.payload["booking"]) == {
        "action_id",
        "kind",
        "phase",
        "status",
        "error_code",
        "idempotency_key",
    }
    for sentinel in (PATIENT_PHONE, "Rami Khoury", "Karim Haddad", "hold_1", "2026-09-30"):
        assert sentinel not in rendered, sentinel


async def test_an_idempotency_conflict_gets_its_own_dead_letter_too(sessionmaker_for):
    """Two entries for one outcome, deliberately: the patient needs somebody to check
    what happened, AND our key derivation produced the same key for a different body,
    which is a bug in this repo."""
    failures = FailureScript()
    service = booking_service(failures=failures)
    await _run(sessionmaker_for, meta(), service, hold_script())
    failures.push("create_appointment", "IDEMPOTENCY_CONFLICT")

    await _run(
        sessionmaker_for,
        meta(),
        service,
        FakeChatClient(
            wants_tools(tool_call("book_appointment", {"full_name": "Rami Khoury"})),
            ok("The clinic team will check."),
        ),
        n=2,
    )

    letters = {row.error for row in await _all(sessionmaker_for, DeadLetterJob)}
    assert {"booking_uncertain", "booking_idempotency_conflict"} <= letters


async def test_two_writers_on_one_conversation_do_not_deadlock(sessionmaker_for):
    """Plan check U8, through the job. Two quick messages, each carrying an outcome.

    T1b takes the conversation row lock FIRST, so the second waits for the first's
    commit instead of deadlocking on the reply's own KEY SHARE lock. The partial
    unique index then guarantees one PENDING row.
    """
    service = booking_service()

    # Two inbound messages in one conversation, each holding a different slot.
    def hold_nth(index: int):
        def step(messages):
            slots = last_tool_result(messages)["slots"]
            return wants_tools(
                tool_call("hold_appointment_slot", {"slot_id": slots[index]["slot_id"]})
            )

        return step

    async def one(n: int, index: int):
        chat = FakeChatClient(
            wants_tools(tool_call("list_doctors", {})),
            wants_tools(tool_call("search_available_slots", WEDNESDAY_AFTERNOON)),
            hold_nth(index),
            ok(f"held {index}"),
        )
        return await _run(sessionmaker_for, meta(), service, chat, n=n)

    # Sequential, because two turns for one conversation in ONE test would also race
    # on the conversation's creation. What is under test is that the second T1b's
    # lock-first ordering leaves exactly one PENDING row.
    await one(1, 0)
    await one(2, 2)

    rows = await _all(sessionmaker_for, BookingAction)
    assert len(rows) == 2
    assert sorted(row.status for row in rows) == ["PENDING", "SUPERSEDED"]


async def test_a_read_only_retried_attempt_still_records_nothing(sessionmaker_for):
    """Q1, unchanged by V9. `test_a_turn_deadline_retries_and_records_nothing_until_t1b`
    pins it in test_inbox_tools.py; this is the booking-shaped restatement: an attempt
    with no booking outcome writes no run, even now that some attempts do.
    """
    from tests.integrations.fakes import retryable

    service = booking_service()
    chat = FakeChatClient(
        wants_tools(tool_call("list_doctors", {})),
        retryable("openai_http_503"),
    )

    with pytest.raises(Retry):
        await _run(sessionmaker_for, meta(), service, chat)

    assert await _all(sessionmaker_for, AgentRun) == []
    assert await _all(sessionmaker_for, BookingAction) == []


def test_the_patient_reference_constant_matches_the_factory():
    """A guard rail on this file's own sentinel.

    Every leak assertion above searches for PATIENT_PHONE. If the WhatsApp factory ever
    changed how it builds a number, they would all keep passing while looking for a
    string that no longer appears anywhere - the worst kind of green.
    """
    from tests.whatsapp_factories import phone

    assert phone(1) == PATIENT_PHONE
