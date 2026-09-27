"""Inbound messages: store everything, reply once.

The Meta transport is always a MockTransport that counts requests, because most
of what is asserted here is "how many times did we send?".
"""

import asyncio
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
from app.worker.jobs.inbox import ACK_TEXT, process_inbox_event
from tests.db import factories as dbf
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
    assert reply.text == ACK_TEXT
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


async def test_the_state_is_re_read_immediately_before_the_send(sessionmaker_for, monkeypatch):
    """Review Focus 10.

    A human taking the conversation over WHILE the job runs must still stop the
    reply. Simulated by flipping the state during the contact upsert, which is
    after get_or_create_open and before the rule-7 re-read.
    """
    from app.db.repositories import ContactRepository

    original = ContactRepository.get_or_create_by_identity
    flipped = {"done": False}

    async def flip_after(self, channel, external_id, display_name=None):
        contact = await original(self, channel, external_id, display_name)
        if not flipped["done"]:
            flipped["done"] = True
            await self._session.execute(
                sa.update(Conversation).values(state=ConversationState.HUMAN_ACTIVE.value)
            )
        return contact

    transport = Meta()
    settings = worker_settings()
    # Open a conversation first, with the flip disabled.
    flipped["done"] = True
    await _run(sessionmaker_for, transport, message_payload(1), settings, n=1)

    flipped["done"] = False
    monkeypatch.setattr(ContactRepository, "get_or_create_by_identity", flip_after)
    _, outcome = await _run(
        sessionmaker_for, transport, message_payload(1, id=wamid(2)), settings, n=2
    )

    assert outcome == "dropped_not_ai_active"
    assert transport.sends == 1


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


async def test_no_log_line_from_the_message_path_contains_patient_content(sessionmaker_for, caplog):
    with caplog.at_level(logging.DEBUG):
        await _run(sessionmaker_for, Meta())

    for value in (PATIENT_TEXT, PROFILE_NAME, phone(1), wamid(1), wamid(9)):
        assert value not in caplog.text
