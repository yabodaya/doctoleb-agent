"""Inbound messages: store everything, reply once.

The Meta transport is always a MockTransport that counts requests, because most
of what is asserted here is "how many times did we send?".
"""

import asyncio
import json
import logging

import httpx
import pytest
import sqlalchemy as sa

from app.db.enums import (
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Contact, Conversation, DeadLetterJob, Message, WebhookInbox
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.integrations.fakes import AI_REPLY, FakeChatClient, ok, permanent, retryable
from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PHONE_NUMBER_ID,
    PROFILE_NAME,
    phone,
    wamid,
)
from tests.worker.conftest import (
    Meta,
    job_context,
    message_payload,
    meta_client,
    ok_response,
    store_event,
    worker_settings,
)

pytestmark = pytest.mark.db


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


# --- storage ----------------------------------------------------------------


async def test_a_text_message_creates_a_contact_a_conversation_and_an_inbound_message(
    sessionmaker_for,
):
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport)

    assert outcome == "replied"
    contact = await _one(sessionmaker_for, Contact)
    assert contact.tenant_id == dbf.TENANT_A
    conversation = await _one(sessionmaker_for, Conversation)
    assert conversation.state == ConversationState.AI_ACTIVE.value
    inbound = await _one(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    assert inbound.text == PATIENT_TEXT
    assert inbound.modality == MessageModality.TEXT.value
    assert inbound.status == MessageStatus.RECEIVED.value


async def test_the_profile_name_is_taken_from_contacts_by_wa_id(sessionmaker_for):
    """The name lives in the change's `contacts` array, not on the message.

    One change can carry several contacts, so it is matched by wa_id rather than
    taken from index 0.
    """
    payload = message_payload(1)
    payload["contacts"] = [
        {"profile": {"name": "Someone Else"}, "wa_id": phone(77)},
        {"profile": {"name": PROFILE_NAME}, "wa_id": phone(1)},
    ]

    await _run(sessionmaker_for, Meta(), payload)

    assert (await _one(sessionmaker_for, Contact)).display_name == PROFILE_NAME


async def test_the_inbound_message_carries_the_wamid_as_its_provider_message_id(sessionmaker_for):
    """The key that makes a re-run safe (hard rule 2)."""
    await _run(sessionmaker_for, Meta())

    inbound = await _one(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    assert inbound.provider_message_id == wamid(1)


async def test_a_second_message_from_the_same_patient_reuses_the_contact_and_conversation(
    sessionmaker_for,
):
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()

    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)
    await _run(sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2)

    assert len(await _all(sessionmaker_for, Contact)) == 1
    assert len(await _all(sessionmaker_for, Conversation)) == 1
    inbound = await _all(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    assert len(inbound) == 2


async def test_an_audio_message_is_stored_as_a_voice_note_with_no_text(sessionmaker_for):
    """VS-008 attaches a transcript to THIS row rather than writing a backfill."""
    payload = message_payload(1, type="audio", text=None, audio={"id": "media-1", "voice": True})

    _, outcome = await _run(sessionmaker_for, Meta(), payload)

    inbound = await _one(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    assert inbound.modality == MessageModality.VOICE_NOTE.value
    assert inbound.text is None
    assert outcome == "stored_no_reply"


async def test_an_image_message_is_stored_with_modality_other(sessionmaker_for):
    """Requirement 2's "store all inbound message types", and plan note C4's
    reason for widening the CHECK constraint."""
    payload = message_payload(1, type="image", text=None, image={"id": "media-2"})

    await _run(sessionmaker_for, Meta(), payload)

    inbound = await _one(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    assert inbound.modality == MessageModality.OTHER.value


async def test_an_image_message_gets_no_reply(sessionmaker_for):
    """Stored, but not answered. The clinic can still see it."""
    payload = message_payload(1, type="image", text=None, image={"id": "media-2"})
    transport = Meta()

    event_id, outcome = await _run(sessionmaker_for, transport, payload)

    assert outcome == "stored_no_reply"
    assert transport.sends == 0
    assert await _all(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value) == []
    row = await _one(sessionmaker_for, WebhookInbox)
    assert row.status == InboxStatus.PROCESSED.value


async def test_the_reply_type_filter_is_read_from_settings(sessionmaker_for):
    payload = message_payload(1, type="image", text=None, image={"id": "media-2"})
    settings = worker_settings(whatsapp_reply_to_types="text, image")
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport, payload, settings)

    assert outcome == "replied"
    assert transport.sends == 1


async def test_a_message_that_fails_its_model_dead_letters(sessionmaker_for):
    """Plan assumption A14: a hash-keyed row has no wamid, so there is nothing to
    make the inbound message idempotent on. VS-003 predicted this."""
    payload = message_payload(1)
    payload["item"] = {"from": phone(1), "type": "text"}  # no id

    _, outcome = await _run(sessionmaker_for, Meta(), payload)

    assert outcome == "dead_lettered"
    assert (await _one(sessionmaker_for, DeadLetterJob)).error == "unmodelled_message"


async def test_a_message_with_no_sender_dead_letters(sessionmaker_for):
    """No wa_id means no contact, no conversation and nobody to reply to."""
    payload = message_payload(1)
    payload["item"] = {"id": wamid(1), "type": "text", "text": {"body": PATIENT_TEXT}}

    _, outcome = await _run(sessionmaker_for, Meta(), payload)

    assert outcome == "dead_lettered"


async def test_a_conversation_race_is_retryable(sessionmaker_for, monkeypatch):
    """VS-002's follow-up, pulled in by requirement 2.

    Another worker won the race between get_open's check and the insert. A retry
    re-reads and finds their conversation; a dead letter would lose the message
    over a race that resolves itself.
    """
    from arq.worker import Retry
    from sqlalchemy.exc import IntegrityError

    from app.db.repositories import ConversationRepository

    async def racing(self, contact_id, channel):
        raise IntegrityError("INSERT", {}, Exception("uq_conversations_open"))

    monkeypatch.setattr(ConversationRepository, "get_or_create_open", racing)

    with pytest.raises(Retry):
        await _run(sessionmaker_for, Meta())

    assert await _all(sessionmaker_for, DeadLetterJob) == []


# --- replying exactly once --------------------------------------------------


async def test_a_text_message_sends_one_reply_and_stores_it_as_sent_with_a_wamid(
    sessionmaker_for,
):
    transport = Meta(ok_response(9))

    event_id, outcome = await _run(sessionmaker_for, transport)

    assert outcome == "replied"
    assert transport.sends == 1
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == AI_REPLY
    assert reply.status == MessageStatus.SENT.value
    assert reply.provider_message_id == wamid(9)
    assert reply.sent_at is not None
    assert (await _one(sessionmaker_for, WebhookInbox)).status == InboxStatus.PROCESSED.value


async def test_the_reply_row_is_linked_to_the_inbound_message(sessionmaker_for):
    await _run(sessionmaker_for, Meta())

    inbound = await _one(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.reply_to_message_id == inbound.id


async def test_the_reply_goes_to_the_phone_number_id_the_message_arrived_on(sessionmaker_for):
    """Assumption A7. With more than one clinic, META_PHONE_NUMBER_ID is simply
    the wrong number."""
    transport = Meta()
    settings = worker_settings(meta_phone_number_id="999999999999999")

    await _run(sessionmaker_for, transport, None, settings)

    url = str(transport.requests[0].url)
    assert PHONE_NUMBER_ID in url
    assert "999999999999999" not in url


async def test_running_the_job_twice_sends_one_reply(sessionmaker_for):
    """The slice's acceptance criterion. The second run stops at the claim."""
    transport = Meta()
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)

    first = await process_inbox_event(ctx, str(event_id))
    second = await process_inbox_event(ctx, str(event_id))

    assert (first, second) == ("replied", "skipped")
    assert transport.sends == 1
    assert len(await _all(sessionmaker_for, Message, direction="OUTBOUND")) == 1


async def test_a_job_whose_reply_row_already_has_a_wamid_sends_nothing(sessionmaker_for):
    """Requirement 3, verbatim: the crash-between-T2's-two-statements case.

    Force the row back to PROCESSING with the reply already sent, which is what a
    process killed just after Meta accepted but before the inbox row was marked
    would leave behind.
    """
    transport = Meta()
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)
    await process_inbox_event(ctx, str(event_id))

    async with sessionmaker_for() as session:
        await session.execute(
            sa.update(WebhookInbox)
            .where(WebhookInbox.id == event_id)
            .values(status=InboxStatus.PROCESSING.value, locked_until=None)
        )
        await session.commit()

    outcome = await process_inbox_event(ctx, str(event_id))

    assert outcome == "already_replied"
    assert transport.sends == 1


async def test_the_reply_row_is_committed_before_the_send(sessionmaker_for, second_session_factory):
    """Review Focus 5, and the commit boundary made executable.

    The transport reads the reply row through an INDEPENDENT session while the
    send is in flight. If the reply row were written in the same transaction as
    the wamid, a crash during the send would leave no trace of an attempted send
    and the retry would have nothing to recognise.
    """
    seen = []

    async def peek(request):
        async with second_session_factory() as other:
            rows = (
                await other.scalars(
                    sa.select(Message).where(Message.direction == MessageDirection.OUTBOUND.value)
                )
            ).all()
            seen.append([(r.status, r.provider_message_id) for r in rows])

    transport = Meta(hook=peek)

    await _run(sessionmaker_for, transport)

    assert seen == [[(MessageStatus.QUEUED.value, None)]]


async def test_a_retryable_send_leaves_the_reply_row_queued_and_retries(sessionmaker_for):
    """So the next try recognises the row and does not reserve a second one."""
    from arq.worker import Retry

    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}))

    with pytest.raises(Retry):
        await _run(sessionmaker_for, transport)

    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.status == MessageStatus.QUEUED.value
    assert reply.provider_message_id is None
    row = await _one(sessionmaker_for, WebhookInbox)
    assert row.status == InboxStatus.PROCESSING.value
    assert row.locked_until is None


async def test_a_retried_send_after_a_failure_reuses_the_same_reply_row(sessionmaker_for):
    """Amendment A2's payoff, end to end.

    The second try re-stores nothing and reserves nothing: the inbound message
    hits its unique constraint and is re-read, and the reply row is returned by
    ON CONFLICT DO NOTHING. Exactly one of each exists afterwards.
    """
    from arq.worker import Retry

    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}), ok_response(9))
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)

    with pytest.raises(Retry):
        await process_inbox_event(ctx, str(event_id))
    outcome = await process_inbox_event(ctx, str(event_id))

    assert outcome == "replied"
    assert transport.sends == 2
    assert len(await _all(sessionmaker_for, Message)) == 2  # one inbound, one reply
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.provider_message_id == wamid(9)


async def test_a_permanent_send_marks_the_reply_failed_and_dead_letters(sessionmaker_for):
    """Hard rule 5's shape: nothing anywhere claims the patient was told
    something they were not."""
    transport = Meta(httpx.Response(400, json={"error": {"code": 131030}}))

    _, outcome = await _run(sessionmaker_for, transport)

    assert outcome == "dead_lettered"
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.status == MessageStatus.FAILED.value
    assert reply.provider_message_id is None
    assert (await _one(sessionmaker_for, DeadLetterJob)).error == "http_400 code_131030"


async def test_a_send_accepted_without_an_id_is_not_resent(sessionmaker_for):
    """The duplicate-reply gap from the other side.

    Meta took the message; retrying would send a second copy. SENT with a NULL
    wamid, and a warning.
    """
    transport = Meta(httpx.Response(200, json={"messaging_product": "whatsapp"}))

    _, outcome = await _run(sessionmaker_for, transport)

    assert outcome == "sent_without_id"
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.status == MessageStatus.SENT.value
    assert reply.provider_message_id is None
    assert (await _one(sessionmaker_for, WebhookInbox)).status == InboxStatus.PROCESSED.value


async def test_two_concurrent_runs_of_one_job_send_exactly_one_reply(
    sessionmaker_for, second_session_factory
):
    """Plan note C3a, end to end at the job level.

    Two runs started together, against a transport that holds the first send open
    long enough for the second to get past its claim if it can. Without the lease
    both claim the PROCESSING row, both find the same reserved reply row with no
    wamid, and both send - the reply row's unique constraint does not help,
    because it prevents two reply ROWS, not two sends from one row.
    """
    from arq.worker import Retry

    started = asyncio.Event()

    async def stall(request):
        started.set()
        await asyncio.sleep(0.2)

    transport = Meta(hook=stall)
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)

    results = await asyncio.gather(
        process_inbox_event(ctx, str(event_id)),
        process_inbox_event(ctx, str(event_id)),
        return_exceptions=True,
    )

    assert transport.sends == 1
    assert len(await _all(sessionmaker_for, Message, direction="OUTBOUND")) == 1
    # One succeeded; the other was told `locked` and deferred.
    assert "replied" in results
    assert any(isinstance(r, Retry) for r in results)


# --- hard rule 7, and privacy -----------------------------------------------


async def _set_state(sessionmaker, state: ConversationState) -> None:
    async with sessionmaker() as session:
        await session.execute(sa.update(Conversation).values(state=state.value))
        await session.commit()


async def test_a_human_active_conversation_drops_the_reply(sessionmaker_for):
    transport = Meta()
    settings = worker_settings()
    # First message opens the conversation; then a human takes it over.
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)
    await _set_state(sessionmaker_for, ConversationState.HUMAN_ACTIVE)

    _, outcome = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2
    )

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 1  # the first message's reply, and nothing since
    assert len(await _all(sessionmaker_for, Message, direction="OUTBOUND")) == 1


async def test_a_closed_conversation_drops_the_reply(sessionmaker_for):
    """A CLOSED conversation is not reused by get_or_create_open.

    Two distinct wamids from the transport, deliberately: messages.
    provider_message_id is globally unique, so replaying one wamid for two replies
    is a unique violation rather than a realistic scenario.
    """
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)
    await _set_state(sessionmaker_for, ConversationState.CLOSED)

    _, outcome = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2
    )

    # A closed conversation frees the partial unique index, so a NEW AI_ACTIVE
    # conversation is opened and answered. That is the documented state machine,
    # not a rule-7 bypass: the dropped case is the one above.
    assert outcome == "replied"
    assert len(await _all(sessionmaker_for, Conversation)) == 2


async def test_the_state_is_re_read_immediately_before_the_send(
    sessionmaker_for, second_session_factory, monkeypatch
):
    """Review Focus 10, and the only version of it that tests anything.

    A human taking the conversation over WHILE the job runs must still stop the
    reply. Three things have to be true of the simulation or it proves nothing:

    * the flip happens immediately AFTER get_or_create_open has loaded the
      conversation into the job's session, because that is the window rule 7
      exists to cover;
    * it happens in an INDEPENDENT session and is COMMITTED, the way a staff
      member taking over in the dashboard would; and
    * the job's own session is never told about it, which is exactly the
      condition under which SQLAlchemy's identity map hands back the stale object
      it already has.

    The earlier version of this test flipped the state inside the job's own
    session during the contact upsert - before the conversation was ever loaded -
    so the re-read had nothing stale to find, and the test passed against a
    re-read that never went to the database.

    VS-005 note: this now proves the FIRST of two hard-rule-7 reads - the one
    inside T1, which keeps a conversation a human already holds from costing a
    model call at all. The SECOND read, the one that actually protects the send,
    is proved by test_a_takeover_during_generation_drops_the_reply.

    The hook is on get_or_create_open's RETURN and not on the step after it:
    MessageRepository.add updates conversations.last_inbound_at, which takes a row
    lock, so an independent session flipping the state after that point blocks on
    the job's uncommitted transaction while the job waits for it - a deadlock in
    the test, not a finding about the code.
    """
    from app.db.repositories import ConversationRepository

    original = ConversationRepository.get_or_create_open
    flipped = {"armed": False}

    async def load_then_flip(self, contact_id, channel):
        conversation = await original(self, contact_id, channel)
        if flipped["armed"]:
            flipped["armed"] = False
            async with second_session_factory() as staff:
                await staff.execute(
                    sa.update(Conversation)
                    .where(Conversation.id == conversation.id)
                    .values(state=ConversationState.HUMAN_ACTIVE.value)
                )
                await staff.commit()
        return conversation

    # Two distinct wamids, so that a second send fails this test's ASSERTION
    # rather than the unique constraint on provider_message_id - the symptom
    # should name the bug, not a scripting artefact.
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    # First message opens the conversation, with the flip disarmed.
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)

    flipped["armed"] = True
    monkeypatch.setattr(ConversationRepository, "get_or_create_open", load_then_flip)
    _, outcome = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2
    )

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 1  # the first message's reply, and nothing since
    assert len(await _all(sessionmaker_for, Message, direction="OUTBOUND")) == 1


async def test_a_dropped_reply_logs_ids_only(sessionmaker_for, caplog):
    """Hard rule 7's own wording: dropped and logged BY ID."""
    transport = Meta()
    settings = worker_settings()
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)
    await _set_state(sessionmaker_for, ConversationState.HUMAN_ACTIVE)

    with caplog.at_level(logging.INFO):
        await _run(sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2)

    dropped = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert dropped
    for value in (PATIENT_TEXT, PROFILE_NAME, phone(1), wamid(2)):
        assert value not in "\n".join(dropped)


async def test_no_log_line_from_the_message_path_contains_patient_or_generated_content(
    sessionmaker_for, caplog
):
    """VS-005 adds two more things that must never be logged: the text the model
    wrote, and the system prompt it was written from."""
    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker_for, Meta(), chat=FakeChatClient(ok()))

    for value in (
        PATIENT_TEXT,
        PROFILE_NAME,
        phone(1),
        wamid(1),
        wamid(9),
        AI_REPLY,
        "You are the WhatsApp receptionist",
    ):
        assert value not in caplog.text


# --- VS-005: the generated reply --------------------------------------------


async def _staff_takeover(second_session_factory):
    """A staff member taking the conversation over, from an independent session.

    SET LOCAL lock_timeout is the second half of every test that uses this: if
    the job still held T1's row lock on the conversation - MessageRepository.add
    updates last_inbound_at - this UPDATE would block for the whole of the model
    call. With the timeout it fails in two seconds and names the bug, instead of
    hanging the suite.
    """
    async with second_session_factory() as staff:
        await staff.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        await staff.execute(
            sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
        )
        await staff.commit()


async def test_a_text_message_is_answered_with_the_generated_text(sessionmaker_for):
    """ACK_TEXT is gone: the reply is whatever the model wrote."""
    transport = Meta()
    chat = FakeChatClient(ok())

    _, outcome = await _run(sessionmaker_for, transport, chat=chat)

    assert outcome == "replied"
    assert len(chat.calls) == 1
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == AI_REPLY
    assert json.loads(transport.requests[0].content)["text"]["body"] == AI_REPLY


async def test_the_model_sees_the_system_prompt_then_the_history_then_the_new_message(
    sessionmaker_for,
):
    """Requirement 5, at the job level: roles, order, and the answered message last.

    VS-006 inserted the clock message (a `system` turn) between the history and
    the message being answered - decision D3, plan conflict C12. The expected
    list below changed deliberately; tests/agent/test_tool_loop.py explains why
    it sits there rather than next to the prompt.
    """
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    chat = FakeChatClient(ok())

    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1, chat=chat)
    await _run(
        sessionmaker_for,
        transport,
        message_payload(1, id=wamid(2), text={"body": "and one more thing"}),
        settings,
        n=2,
        chat=chat,
    )

    second = chat.calls[1]
    assert [m.role for m in second] == ["system", "user", "assistant", "system", "user"]
    assert second[0].content.startswith("You are the WhatsApp receptionist")
    assert [m.content for m in second[1:3]] == [PATIENT_TEXT, AI_REPLY]
    assert second[3].content.startswith("Current date and time at the clinic")
    assert second[4].content == "and one more thing"


async def test_a_failed_reply_is_left_out_of_the_history(sessionmaker_for):
    """Plan conflict C9's other half: a reply that never reached the patient is
    not something the clinic said."""
    transport = Meta(httpx.Response(400, json={"error": {"code": 131030}}), ok_response(9))
    settings = worker_settings()
    chat = FakeChatClient(ok())

    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1, chat=chat)
    await _run(
        sessionmaker_for,
        transport,
        message_payload(1, id=wamid(2), text={"body": "hello again"}),
        settings,
        n=2,
        chat=chat,
    )

    second = chat.calls[1]
    # system prompt, the earlier patient message, the clock (VS-006, D3), then
    # the new message. The failed reply is absent, which is the point.
    assert [m.role for m in second] == ["system", "user", "system", "user"]
    assert AI_REPLY not in [m.content for m in second]


async def test_the_reply_row_holds_the_generated_text_before_the_send(
    sessionmaker_for, second_session_factory
):
    """VS-004's commit boundary, now carrying the decision.

    Once a text is reserved, that text IS the reply: a retry sends what is in
    the row and never asks the model again.
    """
    seen = []

    async def peek(request):
        async with second_session_factory() as other:
            rows = (
                await other.scalars(
                    sa.select(Message).where(Message.direction == MessageDirection.OUTBOUND.value)
                )
            ).all()
            seen.append([(r.status, r.provider_message_id, r.text) for r in rows])

    await _run(sessionmaker_for, Meta(hook=peek), chat=FakeChatClient(ok()))

    assert seen == [[(MessageStatus.QUEUED.value, None, AI_REPLY)]]


async def test_a_retry_after_a_failed_send_sends_the_stored_text_and_never_calls_the_model_again(
    sessionmaker_for,
):
    """Requirement 2, verbatim. No second bill, and no different second answer."""
    from arq.worker import Retry

    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}), ok_response(9))
    settings = worker_settings()
    # The second scripted result is DIFFERENT on purpose: if the job asked
    # again, the reply row and the second send would disagree with the first.
    chat = FakeChatClient(ok(), ok("a different second answer"))
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings, chat=chat)

    with pytest.raises(Retry):
        await process_inbox_event(ctx, str(event_id))
    outcome = await process_inbox_event(ctx, str(event_id))

    assert outcome == "replied"
    assert len(chat.calls) == 1
    assert transport.sends == 2
    bodies = [json.loads(r.content)["text"]["body"] for r in transport.requests]
    assert bodies == [AI_REPLY, AI_REPLY]
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == AI_REPLY


async def test_a_reply_reserved_by_an_earlier_try_is_sent_as_stored(sessionmaker_for):
    """Seeded rather than simulated: whatever put the row there, its text is
    the reply and the model is not consulted."""
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    chat = FakeChatClient(ok())
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1, chat=chat)

    # A second inbound message with a reply row already reserved for it.
    async with sessionmaker_for() as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        inbound = dbf.make_message(
            conversation, text="second message", provider_message_id=wamid(2)
        )
        session.add(inbound)
        await session.flush()
        reserved = dbf.make_reply(conversation, inbound, text="reserved by an earlier try")
        session.add(reserved)
        await session.commit()

    chat.calls.clear()
    _, outcome = await _run(
        sessionmaker_for,
        transport,
        message_payload(1, id=wamid(2), text={"body": "second message"}),
        settings,
        n=2,
        chat=chat,
    )

    assert outcome == "replied"
    assert chat.calls == []
    sent = json.loads(transport.requests[-1].content)["text"]["body"]
    assert sent == "reserved by an earlier try"


async def test_a_takeover_during_generation_drops_the_reply(
    sessionmaker_for, second_session_factory
):
    """The slice's acceptance test.

    The model call is now the longest thing the job does, so the hard-rule-7
    check that protects the send has to come AFTER it. This flips the state
    from an independent session while the fake model is "thinking".

    The lock_timeout inside _staff_takeover is the other half: if the job still
    held T1's row lock on the conversation, the staff UPDATE would fail in two
    seconds instead of hanging the suite - which is how a regression here
    announces itself.
    """
    transport = Meta()

    async def takeover(messages):
        await _staff_takeover(second_session_factory)

    chat = FakeChatClient(ok(), hook=takeover)

    _, outcome = await _run(sessionmaker_for, transport, chat=chat)

    assert outcome == "dropped_not_ai_active"
    assert len(chat.calls) == 1
    assert transport.sends == 0
    assert await _all(sessionmaker_for, Message, direction="OUTBOUND") == []


async def test_a_takeover_during_the_meta_send_is_not_blocked(
    sessionmaker_for, second_session_factory
):
    """No transaction is open during the Meta call either.

    The reply was already on its way when the takeover happened, so it
    completes - what this test proves is that the staff UPDATE did not have to
    wait for it.
    """

    async def takeover(request):
        await _staff_takeover(second_session_factory)

    transport = Meta(hook=takeover)

    _, outcome = await _run(sessionmaker_for, transport, chat=FakeChatClient(ok()))

    assert outcome == "replied"
    assert transport.sends == 1
    async with sessionmaker_for() as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        assert conversation.state == ConversationState.HUMAN_ACTIVE.value


async def test_a_conversation_a_human_holds_is_not_sent_to_the_model(sessionmaker_for):
    """Plan conflict S4's first read: no model call, and none of the patient's
    words sent to OpenAI, for a conversation the AI is not running."""
    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    chat = FakeChatClient(ok())
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1, chat=chat)
    await _set_state(sessionmaker_for, ConversationState.HUMAN_ACTIVE)
    chat.calls.clear()

    _, outcome = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2, chat=chat
    )

    assert outcome == "dropped_not_ai_active"
    assert chat.calls == []


async def test_a_reply_reserved_before_a_takeover_is_marked_failed_not_left_queued(
    sessionmaker_for,
):
    """Plan conflict C9.

    A row left QUEUED forever would reach later prompts as something the clinic
    said. FAILED is also hard rule 5's shape: nothing claims the patient was
    told it.
    """
    transport = Meta(ok_response(8))
    settings = worker_settings()
    chat = FakeChatClient(ok())
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1, chat=chat)

    async with sessionmaker_for() as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        inbound = dbf.make_message(conversation, text="second", provider_message_id=wamid(2))
        session.add(inbound)
        await session.flush()
        session.add(dbf.make_reply(conversation, inbound, text="never sent"))
        await session.commit()
    await _set_state(sessionmaker_for, ConversationState.HUMAN_ACTIVE)
    chat.calls.clear()

    _, outcome = await _run(
        sessionmaker_for,
        transport,
        message_payload(1, id=wamid(2), text={"body": "second"}),
        settings,
        n=2,
        chat=chat,
    )

    assert outcome == "dropped_not_ai_active"
    assert chat.calls == []
    stranded = await _one(sessionmaker_for, Message, text="never sent")
    assert stranded.status == MessageStatus.FAILED.value
    assert stranded.provider_message_id is None


async def test_the_job_never_reads_the_state_through_conversation_get(
    sessionmaker_for, monkeypatch
):
    """Requirement 2's warning, made executable.

    ConversationRepository.get selects the mapped entity, which in a session
    that has already loaded the conversation is answered from SQLAlchemy's
    identity map - so a "re-read" through it would never see a commit made by
    anyone else, which is the only thing it exists to see.
    """
    from app.db.repositories import ConversationRepository

    def explode(self, conversation_id):
        raise AssertionError("hard rule 7 must read current_state, never get")

    monkeypatch.setattr(ConversationRepository, "get", explode)

    transport = Meta(ok_response(8), ok_response(9))
    settings = worker_settings()
    _, replied = await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)
    await _set_state(sessionmaker_for, ConversationState.HUMAN_ACTIVE)
    _, dropped = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2
    )

    assert (replied, dropped) == ("replied", "dropped_not_ai_active")


async def test_a_retryable_generation_failure_with_tries_left_retries_and_reserves_nothing(
    sessionmaker_for,
):
    """Nothing is reserved, so the next try starts clean and asks again.

    This stays true after Task 7, which only changes what happens on the LAST
    try.
    """
    from arq.worker import Retry

    with pytest.raises(Retry):
        await _run(sessionmaker_for, Meta(), chat=FakeChatClient(retryable()), job_try=1)

    assert await _all(sessionmaker_for, Message, direction="OUTBOUND") == []
    assert await _all(sessionmaker_for, DeadLetterJob) == []
    row = await _one(sessionmaker_for, WebhookInbox)
    assert row.status == InboxStatus.PROCESSING.value
    assert row.locked_until is None


async def test_a_permanent_generation_failure_sends_the_fallback_on_the_first_try(
    sessionmaker_for,
):
    """Requirement 4. No credit is not something a retry can fix, so the patient
    is answered now rather than 75 seconds of backoff later."""
    transport = Meta()
    settings = worker_settings()
    chat = FakeChatClient(permanent())

    _, outcome = await _run(sessionmaker_for, transport, None, settings, chat=chat, job_try=1)

    assert outcome == "replied_fallback"
    assert len(chat.calls) == 1
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == settings.agent_fallback_reply
    assert reply.status == MessageStatus.SENT.value
    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert letter.error == "openai_insufficient_quota"
    assert letter.attempts == 1
    assert (await _one(sessionmaker_for, WebhookInbox)).status == InboxStatus.PROCESSED.value


async def test_the_generation_log_line_carries_codes_counts_and_the_row_id_only(
    sessionmaker_for, caplog
):
    """Plan assumption A5: one line per generation, and one grep for AI problems."""
    with caplog.at_level(logging.INFO):
        event_id, _ = await _run(sessionmaker_for, Meta(), chat=FakeChatClient(ok()))

    lines = [r.getMessage() for r in caplog.records if "reply generated" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    for fragment in (
        f"event_id={event_id}",
        "outcome=SUCCESS",
        "reason=ok",
        "prompt_version=vs006-1",
        "prompt_tokens=11",
        "completion_tokens=7",
    ):
        assert fragment in line, fragment
    for secret in (PATIENT_TEXT, AI_REPLY, "You are the WhatsApp receptionist"):
        assert secret not in line


# --- VS-005: when the model fails -------------------------------------------


async def test_the_next_try_after_a_generation_failure_generates_again(sessionmaker_for):
    """Nothing was reserved, so the next try starts clean."""
    from arq.worker import Retry

    transport = Meta()
    settings = worker_settings()
    chat = FakeChatClient(retryable(), ok())
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings, chat=chat)

    with pytest.raises(Retry):
        await process_inbox_event(ctx, str(event_id))
    outcome = await process_inbox_event(ctx, str(event_id))

    assert outcome == "replied"
    assert len(chat.calls) == 2
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == AI_REPLY
    assert await _all(sessionmaker_for, DeadLetterJob) == []


async def test_a_retryable_generation_failure_on_the_last_try_sends_the_fallback(sessionmaker_for):
    """Requirement 4: out of tries is answered, not silently abandoned.

    job_try == job_max_tries is exactly the comparison the envelope makes to
    decide "dead-letter instead of defer", which is why EventContext carries
    job_try (plan assumption A7) - the two cannot disagree about which try is
    last.
    """
    transport = Meta()
    settings = worker_settings()
    chat = FakeChatClient(retryable())

    _, outcome = await _run(
        sessionmaker_for, transport, None, settings, chat=chat, job_try=settings.job_max_tries
    )

    assert outcome == "replied_fallback"
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == settings.agent_fallback_reply
    assert reply.status == MessageStatus.SENT.value
    assert reply.provider_message_id == wamid(9)
    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert letter.error == "openai_http_503"
    assert letter.attempts == settings.job_max_tries
    assert (await _one(sessionmaker_for, WebhookInbox)).status == InboxStatus.PROCESSED.value


async def test_an_unset_model_sends_the_fallback_without_calling_openai(sessionmaker_for):
    """Requirement 7, end to end, through the REAL client.

    The fake cannot prove this one: what is being asserted is that no HTTP
    request is made at all when OPENAI_CHAT_MODEL is blank.
    """
    import httpx2

    from app.integrations.openai.chat import OpenAIChatClient

    openai_requests: list[httpx2.Request] = []

    async def record(request: httpx2.Request) -> httpx2.Response:
        openai_requests.append(request)
        return httpx2.Response(500, json={"error": {"message": "should never happen"}})

    settings = worker_settings(
        openai_api_key="sk-test-not-a-real-one",
        openai_chat_model="",
    )
    chat = OpenAIChatClient(
        settings, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    )
    transport = Meta()

    _, outcome = await _run(sessionmaker_for, transport, None, settings, chat=chat)

    assert outcome == "replied_fallback"
    assert openai_requests == []
    assert transport.sends == 1
    body = json.loads(transport.requests[0].content)["text"]["body"]
    assert body == settings.agent_fallback_reply
    assert (await _one(sessionmaker_for, DeadLetterJob)).error == "openai_model_unset"


async def test_the_fallback_and_its_dead_letter_are_committed_together_before_the_send(
    sessionmaker_for, second_session_factory
):
    """Plan conflict C7's reason for putting the dead letter in T1b.

    Written earlier, in its own transaction, a crash before the reservation
    would leave a dead letter for a failure that had since healed. Written
    later, with the wamid, a crash during the send would lose it. In T1b the
    two facts - "the fallback is the reply" and "generation failed for this
    reason" - commit together or not at all.
    """
    seen = []
    settings = worker_settings()

    async def peek(request):
        async with second_session_factory() as other:
            replies = (
                await other.scalars(
                    sa.select(Message).where(Message.direction == MessageDirection.OUTBOUND.value)
                )
            ).all()
            letters = (await other.scalars(sa.select(DeadLetterJob))).all()
            seen.append(
                (
                    [(r.status, r.text) for r in replies],
                    [letter.error for letter in letters],
                )
            )

    await _run(sessionmaker_for, Meta(hook=peek), None, settings, chat=FakeChatClient(permanent()))

    assert seen == [
        (
            [(MessageStatus.QUEUED.value, settings.agent_fallback_reply)],
            ["openai_insufficient_quota"],
        )
    ]


async def test_a_crash_after_the_fallback_is_reserved_neither_loses_nor_repeats_its_dead_letter(
    sessionmaker_for,
):
    """The exactly-once property the T1b placement buys.

    A plain RuntimeError from the transport is a crash, not a Meta failure: it
    escapes the envelope's two except blocks entirely, which is the closest a
    test can get to the process being killed mid-send.
    """
    settings = worker_settings()
    chat = FakeChatClient(permanent())
    crashed = {"done": False}

    async def crash_once(request):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("the worker died mid-send")

    transport = Meta(hook=crash_once)
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings, chat=chat)

    with pytest.raises(RuntimeError):
        await process_inbox_event(ctx, str(event_id))

    # The lease is still held by the dead run; clear it the way VS-004's tests do.
    async with sessionmaker_for() as session:
        await session.execute(
            sa.update(WebhookInbox).where(WebhookInbox.id == event_id).values(locked_until=None)
        )
        await session.commit()

    outcome = await process_inbox_event(ctx, str(event_id))

    # `replied`, not `replied_fallback`: the outcome code says what THIS run
    # did, and this run sent a stored reply without generating anything. The
    # dead letter written by the crashed run is where the generation failure is
    # recorded - which is the whole point of committing it with the reservation.
    assert outcome == "replied"
    assert len(chat.calls) == 1
    letters = await _all(sessionmaker_for, DeadLetterJob)
    assert [letter.error for letter in letters] == ["openai_insufficient_quota"]
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.text == settings.agent_fallback_reply
    assert reply.status == MessageStatus.SENT.value


async def test_a_retried_fallback_send_does_not_call_the_model_or_write_a_second_dead_letter(
    sessionmaker_for,
):
    """The fallback goes through the SAME exactly-once path as any reply."""
    from arq.worker import Retry

    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}), ok_response(9))
    settings = worker_settings()
    chat = FakeChatClient(permanent())
    event_id = await store_event(sessionmaker_for, message_payload())
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings, chat=chat)

    with pytest.raises(Retry):
        await process_inbox_event(ctx, str(event_id))
    ctx["job_try"] = 2
    outcome = await process_inbox_event(ctx, str(event_id))

    assert outcome == "replied"
    assert len(chat.calls) == 1
    assert len(await _all(sessionmaker_for, DeadLetterJob)) == 1
    bodies = [json.loads(r.content)["text"]["body"] for r in transport.requests]
    assert bodies == [settings.agent_fallback_reply] * 2


async def test_a_fallback_refused_by_meta_leaves_two_dead_letters_with_two_reasons(
    sessionmaker_for,
):
    """Two different things went wrong, and each has a different fix."""
    transport = Meta(httpx.Response(400, json={"error": {"code": 131030}}))

    _, outcome = await _run(sessionmaker_for, transport, chat=FakeChatClient(permanent()))

    assert outcome == "dead_lettered"
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.status == MessageStatus.FAILED.value
    errors = sorted(letter.error for letter in await _all(sessionmaker_for, DeadLetterJob))
    assert errors == ["http_400 code_131030", "openai_insufficient_quota"]


async def test_a_generation_failure_then_a_takeover_keeps_the_dead_letter_and_sends_nothing(
    sessionmaker_for, second_session_factory
):
    """Hard rule 7 still wins over the fallback - and the failure is still
    recorded, because it still happened and somebody still has to fix it."""

    async def takeover(messages):
        await _staff_takeover(second_session_factory)

    transport = Meta()
    chat = FakeChatClient(permanent(), hook=takeover)

    _, outcome = await _run(sessionmaker_for, transport, chat=chat)

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 0
    assert await _all(sessionmaker_for, Message, direction="OUTBOUND") == []
    assert (await _one(sessionmaker_for, DeadLetterJob)).error == "openai_insufficient_quota"


async def test_a_reply_that_runs_out_of_send_tries_is_marked_failed(sessionmaker_for):
    """Plan conflict C9, on the Meta side.

    The envelope is about to dead-letter this event, so no later try will ever
    send this row. Left QUEUED it would reach later prompts as something the
    clinic said.
    """
    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}))
    settings = worker_settings()

    _, outcome = await _run(
        sessionmaker_for, transport, None, settings, job_try=settings.job_max_tries
    )

    assert outcome == "dead_lettered"
    reply = await _one(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert reply.status == MessageStatus.FAILED.value
    assert reply.provider_message_id is None


async def test_a_generation_dead_letter_carries_codes_and_references_only(sessionmaker_for):
    """Hard rule 8. dead_letter_jobs is a table people open casually to triage."""
    _, _ = await _run(sessionmaker_for, Meta(), chat=FakeChatClient(permanent()))

    letter = await _one(sessionmaker_for, DeadLetterJob)
    assert set(letter.payload) == {"inbox_row_id", "kind", "phone_number_id", "job_try"}
    assert letter.error == "openai_insufficient_quota"
    assert letter.tenant_id == dbf.TENANT_A
    serialised = f"{letter.payload}{letter.error}{letter.source_event_id}{letter.job_name}"
    for secret in (PATIENT_TEXT, AI_REPLY, wamid(1), wamid(9), PROFILE_NAME):
        assert secret not in serialised
