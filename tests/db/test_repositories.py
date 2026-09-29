"""Repositories. The database enforces the rules; these make them convenient
and make tenant_id impossible to forget."""

import datetime as dt
import traceback
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Contact, ContactIdentity, Conversation, WebhookInbox
from app.db.repositories import (
    ContactRepository,
    ConversationRepository,
    DeadLetterJobRepository,
    MessageRepository,
    WebhookInboxRepository,
)
from app.db.repositories.errors import DuplicateRecordError, as_duplicate
from tests.db import factories as f

pytestmark = pytest.mark.db


async def test_get_or_create_by_identity_creates_a_contact_and_an_identity(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1), display_name="Test Patient"
    )
    assert contact.tenant_id == f.TENANT_A
    identity_count = await db_session.scalar(
        sa.select(sa.func.count()).select_from(ContactIdentity)
    )
    assert identity_count == 1


async def test_get_or_create_by_identity_is_idempotent(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    first = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    second = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    assert first.id == second.id


async def test_two_tenants_with_the_same_phone_get_separate_contacts(db_session):
    a = await ContactRepository(db_session, f.TENANT_A).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    b = await ContactRepository(db_session, f.TENANT_B).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    assert a.id != b.id


async def test_a_contact_lookup_returns_nothing_for_another_tenants_id(db_session):
    # Review Focus 6. A correct-looking id from the wrong tenant must produce no
    # data, not another clinic's patient.
    a = await ContactRepository(db_session, f.TENANT_A).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    assert await ContactRepository(db_session, f.TENANT_B).get(a.id) is None
    assert await ContactRepository(db_session, f.TENANT_A).get(a.id) is not None


async def test_a_duplicate_identity_error_never_carries_the_phone_number(db_session):
    # Review Focus 1. The unique key contains the patient's phone number, and
    # asyncpg puts conflicting values into the exception message. One
    # logger.exception() would ship that to an error tracker (hard rule 8).
    contact = f.make_contact(f.TENANT_A)
    db_session.add(contact)
    await db_session.flush()
    db_session.add(f.make_identity(contact, n=7))
    await db_session.flush()

    other = f.make_contact(f.TENANT_A)
    db_session.add(other)
    await db_session.flush()
    db_session.add(f.make_identity(other, n=7))

    with pytest.raises(IntegrityError) as raised:
        await db_session.flush()

    # Where the leak actually is, now that both engines set hide_parameters=True:
    # that setting removes the `[parameters: ...]` appendix, but PostgreSQL's
    # DETAIL line quotes the conflicting key and travels on the chained asyncpg
    # exception. A formatted traceback is what logger.exception() prints, chain
    # included, so that is the thing that must not reach a log or a tracker.
    raw_error = raised.value
    assert f.phone(7) in "".join(traceback.format_exception(raw_error))

    translated = as_duplicate(raw_error)
    assert isinstance(translated, DuplicateRecordError)
    assert f.phone(7) not in str(translated)
    assert f.phone(7) not in repr(translated)
    assert f.phone(7) not in "".join(traceback.format_exception(translated))
    # Proves the driver exception was actually reached: a failed unwrap would
    # fall back to "unknown constraint" and this assertion would catch it.
    assert translated.constraint == "uq_contact_identities_identity"


async def test_get_or_create_open_conversation_starts_in_ai_active(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    assert conversation.state == ConversationState.AI_ACTIVE


async def test_get_or_create_open_conversation_returns_the_existing_one(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    first = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    second = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    assert first.id == second.id


async def test_a_closed_conversation_is_not_reused(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    first = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    await conversations.set_state(first.id, ConversationState.CLOSED)
    second = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    assert second.id != first.id
    assert await conversations.get_open(contact.id, Channel.WHATSAPP) is not None


async def test_set_state_records_when_the_state_changed(db_session):
    # Hard rule 7 re-reads this state right before every send; VS-010 races
    # against it. Knowing when it flipped is what makes that debuggable by id.
    #
    # state_changed_at is backdated first on purpose: now() is the TRANSACTION
    # start time, so without this the "before" and "after" values are the same
    # constant and the assertion could never fail.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    conversation = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)

    await db_session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(state_changed_at=f.LONG_AGO)
    )
    await db_session.refresh(conversation)
    assert conversation.state_changed_at == f.LONG_AGO

    updated = await conversations.set_state(conversation.id, ConversationState.HUMAN_ACTIVE)
    assert updated is not None
    assert updated.state == ConversationState.HUMAN_ACTIVE
    assert updated.state_changed_at > f.LONG_AGO


async def test_a_conversation_lookup_is_tenant_scoped(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )

    assert await ConversationRepository(db_session, f.TENANT_B).get(conversation.id) is None
    assert (
        await ConversationRepository(db_session, f.TENANT_B).set_state(
            conversation.id, ConversationState.CLOSED
        )
        is None
    )


async def test_an_inbound_message_stamps_last_inbound_at(db_session):
    # The 24h WhatsApp free-form window is measured from this value
    # (docs/architecture.md).
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    assert conversation.last_inbound_at is None

    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.RECEIVED,
        text="synthetic message body",
        provider_message_id=f.wamid(1),
    )
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at is not None
    assert conversation.last_inbound_at.tzinfo is not None


async def test_an_outbound_message_does_not_move_last_inbound_at(db_session):
    # Backdated first: now() is constant within the transaction, so comparing
    # the column against itself after an outbound send would pass even if the
    # repository wrongly stamped it.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    conversation = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)

    await db_session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(last_inbound_at=f.LONG_AGO)
    )
    await db_session.refresh(conversation)

    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.QUEUED,
        text="synthetic reply",
    )
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == f.LONG_AGO


async def test_recent_returns_the_newest_messages_oldest_first(db_session):
    # VS-005 feeds this straight into the chat history, which must read in the
    # order it happened. created_at is set explicitly because every row in this
    # transaction would otherwise share one now().
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    messages = MessageRepository(db_session, f.TENANT_A)

    for index in range(5):
        message = await messages.add(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            modality=MessageModality.TEXT,
            status=MessageStatus.RECEIVED,
            text=f"synthetic {index}",
            provider_message_id=f.wamid(index),
        )
        message.created_at = f.LONG_AGO + dt.timedelta(minutes=index)
    await db_session.flush()

    recent = await messages.recent(conversation.id, limit=3)
    assert [m.text for m in recent] == ["synthetic 2", "synthetic 3", "synthetic 4"]


async def test_a_message_lookup_by_provider_id_is_tenant_scoped(db_session):
    # VS-004 uses this to apply Meta status callbacks. A callback for another
    # tenant's message must find nothing.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.SENT,
        provider_message_id=f.wamid(1),
    )

    assert (
        await MessageRepository(db_session, f.TENANT_A).get_by_provider_id(f.wamid(1)) is not None
    )
    assert await MessageRepository(db_session, f.TENANT_B).get_by_provider_id(f.wamid(1)) is None


async def test_a_duplicate_provider_message_id_becomes_a_safe_error(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    messages = MessageRepository(db_session, f.TENANT_A)
    kwargs = {
        "conversation_id": conversation.id,
        "direction": MessageDirection.INBOUND,
        "modality": MessageModality.TEXT,
        "status": MessageStatus.RECEIVED,
        "provider_message_id": f.wamid(1),
    }
    await messages.add(text="first", **kwargs)

    with pytest.raises(DuplicateRecordError) as raised:
        await messages.add(text="my knee hurts", **kwargs)
    assert "my knee hurts" not in str(raised.value)
    assert raised.value.constraint == "uq_messages_provider_message_id"


async def test_store_if_new_returns_none_for_a_replayed_event(db_session):
    # Hard rule 2, as the webhook will use it in VS-003: a None result means
    # "already handled, return 200 and do nothing".
    inbox = WebhookInboxRepository(db_session)
    first = await inbox.store_if_new(f.event_id(1), {"n": 1})
    assert first is not None
    assert first.status == InboxStatus.RECEIVED
    assert await inbox.store_if_new(f.event_id(1), {"n": 1}) is None

    count = await db_session.scalar(sa.select(sa.func.count()).select_from(type(first)))
    assert count == 1


async def test_a_dead_letter_job_may_have_no_tenant(db_session):
    # Hard rule 11. A job that died before tenant resolution is exactly the
    # failure most worth recording, so tenant_id cannot be required here.
    job = await DeadLetterJobRepository(db_session).add(
        job_name="process_whatsapp_event",
        payload={"n": 1},
        error="BookingTimeout",
        attempts=5,
        source_event_id=f.event_id(1),
    )
    assert job.tenant_id is None

    with_tenant = await DeadLetterJobRepository(db_session).add(
        job_name="process_whatsapp_event",
        payload={"n": 2},
        error="BookingTimeout",
        attempts=5,
        tenant_id=f.TENANT_B,
    )
    assert with_tenant.tenant_id == f.TENANT_B


async def test_get_or_create_by_identity_yields_to_the_winner_of_a_race(db_session, monkeypatch):
    # The lost-race branch: the identity already exists, but this call's first
    # get_by_identity misses it — which is exactly what a concurrent writer that
    # committed a moment later looks like. The ON CONFLICT DO NOTHING insert then
    # returns no row, and the repository must abandon the contact it just made and
    # return the winner's. Otherwise one patient becomes two contacts, and every
    # later conversation lookup picks whichever one it happens to find.
    contacts = ContactRepository(db_session, f.TENANT_A)
    original = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))

    real_get_by_identity = ContactRepository.get_by_identity
    calls = {"n": 0}

    async def miss_once(self, channel, external_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_get_by_identity(self, channel, external_id)

    monkeypatch.setattr(ContactRepository, "get_by_identity", miss_once)

    loser = ContactRepository(db_session, f.TENANT_A)
    returned = await loser.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))

    assert calls["n"] == 2  # missed, then re-read after the conflict
    assert returned.id == original.id
    surviving = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Contact).where(Contact.tenant_id == f.TENANT_A)
    )
    assert surviving == 1


# --- the claim, and the lease that makes it safe under concurrency ----------


LEASE = 90.0


async def _stored_inbox(session, n: int = 1, **overrides):
    row = f.make_inbox(n, **overrides)
    session.add(row)
    await session.flush()
    return row


async def test_claiming_a_received_row_returns_it_and_marks_it_processing(db_session):
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)

    claim = await inbox.claim(row.id, LEASE)

    assert claim.state == "claimed"
    assert claim.row is not None
    assert claim.row.status == InboxStatus.PROCESSING.value


async def test_claiming_increments_attempts(db_session):
    """The attempt count is what a dead letter reports.

    Incremented by the claim itself, and committed by the caller straight away
    (plan amendment A1), so a try that dies before it can record anything still
    counts.
    """
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)

    first = await inbox.claim(row.id, LEASE)
    assert first.row.attempts == 1

    await inbox.release(row.id)
    second = await inbox.claim(row.id, LEASE)
    assert second.row.attempts == 2


async def test_claiming_a_processed_row_reports_already_processed(db_session):
    """The worker-side idempotency gate (VS-004 requirement 1).

    arq's job-id dedup is short-lived - results expire - so this is the check
    that holds forever.
    """
    row = await _stored_inbox(db_session, status=InboxStatus.PROCESSED.value)
    inbox = WebhookInboxRepository(db_session)

    claim = await inbox.claim(row.id, LEASE)

    assert claim.state == "already_processed"
    assert claim.row is None


async def test_claiming_a_processing_row_with_no_lease_succeeds(db_session):
    """A crashed previous try MUST be reclaimable.

    The job leaves the row PROCESSING across the Meta call on purpose, so a try
    that died there left it PROCESSING with a lease. Once that lease is gone,
    refusing the row would mean the patient's message is never answered - which
    is why the guard is "status <> PROCESSED" and not "status = RECEIVED".
    """
    row = await _stored_inbox(db_session, status=InboxStatus.PROCESSING.value)
    inbox = WebhookInboxRepository(db_session)

    assert (await inbox.claim(row.id, LEASE)).state == "claimed"


async def test_claiming_a_missing_row_reports_missing(db_session):
    inbox = WebhookInboxRepository(db_session)

    assert (await inbox.claim(uuid.uuid4(), LEASE)).state == "missing"


async def test_claiming_sets_a_lease_in_the_future_measured_by_the_database(db_session):
    """Plan assumption A16: the lease is PostgreSQL's clock, not a worker's.

    Computed server-side inside the claiming UPDATE, so a worker with a skewed
    clock cannot grant itself a longer lease and two workers never have to agree
    on the time - only the database does. Compared against the same
    transaction's now(), which is why it is read here rather than in Python.
    """
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)

    claim = await inbox.claim(row.id, LEASE)
    now = await db_session.scalar(sa.select(sa.func.now()))

    assert claim.row.locked_until > now
    assert (claim.row.locked_until - now).total_seconds() == pytest.approx(LEASE, abs=5)


async def test_a_row_with_a_live_lease_is_not_claimable(db_session):
    """Plan note C3a, at the layer that fixes it.

    attempts must NOT move: another worker's try is not this job's try, and a
    dead letter's count has to be the number of times WE tried.
    """
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)
    await inbox.claim(row.id, LEASE)

    second = await inbox.claim(row.id, LEASE)

    assert second.state == "locked"
    assert second.row is None
    refreshed = await inbox.get(row.id)
    assert refreshed.attempts == 1


async def test_a_row_with_an_expired_lease_is_claimable(db_session):
    """Otherwise one SIGKILL would strand a patient's message forever.

    The lease is a bounded delay, not a tombstone.
    """
    row = await _stored_inbox(
        db_session, status=InboxStatus.PROCESSING.value, locked_until=f.LONG_AGO
    )
    inbox = WebhookInboxRepository(db_session)

    assert (await inbox.claim(row.id, LEASE)).state == "claimed"


async def test_releasing_clears_the_lease_without_touching_the_status(db_session):
    """release() runs on the retry path, where the row must stay PROCESSING.

    Clearing the status as well would be indistinguishable from a fresh event,
    and holding the lease instead would make the deferred retry find its own
    stale lease and defer again.
    """
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)
    await inbox.claim(row.id, LEASE)

    await inbox.release(row.id)

    refreshed = await inbox.get(row.id)
    assert refreshed.locked_until is None
    assert refreshed.status == InboxStatus.PROCESSING.value


async def test_marking_a_row_processed_also_clears_the_lease(db_session):
    row = await _stored_inbox(db_session)
    inbox = WebhookInboxRepository(db_session)
    await inbox.claim(row.id, LEASE)

    await inbox.mark(row.id, InboxStatus.PROCESSED)

    refreshed = await inbox.get(row.id)
    assert refreshed.status == InboxStatus.PROCESSED.value
    assert refreshed.locked_until is None


async def test_get_by_event_id_finds_the_row_the_webhook_could_not_insert(db_session):
    """The webhook's duplicate path (plan note C2).

    store_if_new returns None on conflict, but the redelivery is still enqueued -
    by the SAME row id as last time, which is what lets arq's job id suppress the
    repeat.
    """
    row = await _stored_inbox(db_session, 7)
    inbox = WebhookInboxRepository(db_session)

    assert (await inbox.get_by_event_id(f.event_id(7))).id == row.id
    assert await inbox.get_by_event_id("evt-does-not-exist") is None


async def test_two_concurrent_claims_of_one_row_yield_one_claimed_and_one_locked(
    db_session, second_session_factory
):
    """Plan note C3a, proven on genuinely independent connections.

    db_session cannot show this: it wraps everything in one transaction, so two
    "workers" on it would share a snapshot and neither would block. With two real
    connections the second claim sees a committed live lease and is told "locked"
    at once, rather than sending a second copy of the reply.
    """
    async with second_session_factory() as setup:
        row = f.make_inbox(42)
        setup.add(row)
        await setup.commit()
        row_id = row.id

    try:
        async with second_session_factory() as one, second_session_factory() as two:
            first = await WebhookInboxRepository(one).claim(row_id, LEASE)
            await one.commit()

            second = await WebhookInboxRepository(two).claim(row_id, LEASE)
            await two.commit()

        assert first.state == "claimed"
        assert second.state == "locked"
    finally:
        async with second_session_factory() as cleanup:
            await cleanup.execute(sa.delete(WebhookInbox).where(WebhookInbox.id == row_id))
            await cleanup.commit()


# --- the reply row, and the status ladder -----------------------------------


async def _conversation_with_inbound(session, tenant=f.TENANT_A):
    contact = f.make_contact(tenant)
    session.add(contact)
    await session.flush()
    conversation = f.make_conversation(contact)
    session.add(conversation)
    await session.flush()
    inbound = f.make_message(conversation, provider_message_id=f.wamid(1))
    session.add(inbound)
    await session.flush()
    return conversation, inbound


async def test_reserving_a_reply_twice_returns_the_same_row(db_session):
    """ON CONFLICT DO NOTHING, so a second job gets the first job's row.

    No IntegrityError escapes, which matters twice over: the conflicting values
    are the reply text and conversation ids (hard rule 8), and a failed INSERT
    would abort the whole transaction (plan amendment A2), breaking the very
    re-read this method performs.
    """
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)

    first = await messages.reserve_reply(conversation.id, inbound.id, "Received")
    second = await messages.reserve_reply(conversation.id, inbound.id, "Received")

    assert first.id == second.id
    assert second.status == MessageStatus.QUEUED.value
    assert second.provider_message_id is None


async def test_the_session_still_works_after_a_duplicate_message_insert(db_session):
    """Plan amendment A2, as a standalone proof.

    In PostgreSQL a failed INSERT aborts the WHOLE transaction. Without the
    savepoint in MessageRepository.add, catching DuplicateRecordError would leave
    a session that raises InFailedSqlTransaction on the very re-read it was
    caught to allow - and VS-004's job is built on exactly that re-read.
    """
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)

    with pytest.raises(DuplicateRecordError):
        await messages.add(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            modality=MessageModality.TEXT,
            status=MessageStatus.RECEIVED,
            provider_message_id=f.wamid(1),
        )

    # The session must still be usable, and must still see the original row.
    found = await messages.get_by_provider_id(f.wamid(1))
    assert found.id == inbound.id


async def test_the_session_still_works_after_a_conversation_race(db_session):
    """Plan amendment A2, for the path that deliberately keeps raising.

    get_or_create_open's IntegrityError still propagates - VS-004's job turns it
    into a retry - but the savepoint means the session it propagates through is
    still usable, so the caller can roll back cleanly instead of hitting
    InFailedSqlTransaction on its way out.
    """
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()
    conversations = ConversationRepository(db_session, f.TENANT_A)
    await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)

    # Force the race: insert a second open conversation behind the repository's
    # check, which is what a concurrent worker does.
    db_session.add(f.make_conversation(contact))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_attaching_a_provider_id_marks_the_reply_sent_with_a_timestamp(db_session):
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)
    reply = await messages.reserve_reply(conversation.id, inbound.id, "Received")

    await messages.attach_provider_id(reply.id, f.wamid(2))

    stored = await messages.get_by_provider_id(f.wamid(2))
    assert stored.id == reply.id
    assert stored.status == MessageStatus.SENT.value
    assert stored.sent_at is not None


async def test_a_status_only_moves_forward(db_session):
    """VS-004 requirement 5. Meta redelivers out of order as a matter of course.

    The guard is in the WHERE clause, so a late "delivered" after "read" is a
    no-op decided by the database rather than by a read-then-write in Python.
    """
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)
    reply = await messages.reserve_reply(conversation.id, inbound.id, "Received")
    await messages.attach_provider_id(reply.id, f.wamid(2))

    assert await messages.advance_status(f.wamid(2), MessageStatus.DELIVERED) is True
    assert await messages.advance_status(f.wamid(2), MessageStatus.READ) is True
    assert await messages.advance_status(f.wamid(2), MessageStatus.DELIVERED) is False
    assert await messages.advance_status(f.wamid(2), MessageStatus.READ) is False

    stored = await messages.get_by_provider_id(f.wamid(2))
    assert stored.status == MessageStatus.READ.value


async def test_a_failed_status_overwrites_sent_but_not_delivered(db_session):
    """The FAILED rank decision, both halves.

    A send Meta later reports as failed must overwrite SENT; a message that was
    actually delivered did not fail.
    """
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)
    reply = await messages.reserve_reply(conversation.id, inbound.id, "Received")
    await messages.attach_provider_id(reply.id, f.wamid(2))

    assert await messages.advance_status(f.wamid(2), MessageStatus.FAILED) is True

    await messages.advance_status(f.wamid(2), MessageStatus.DELIVERED)
    assert await messages.advance_status(f.wamid(2), MessageStatus.FAILED) is False
    stored = await messages.get_by_provider_id(f.wamid(2))
    assert stored.status == MessageStatus.DELIVERED.value


async def test_advancing_a_status_for_another_tenant_changes_nothing(db_session):
    """The tenant filter is in the statement, not in the caller (hard rule 4)."""
    conversation, inbound = await _conversation_with_inbound(db_session)
    messages = MessageRepository(db_session, f.TENANT_A)
    reply = await messages.reserve_reply(conversation.id, inbound.id, "Received")
    await messages.attach_provider_id(reply.id, f.wamid(2))

    other_tenant = MessageRepository(db_session, f.TENANT_B)
    assert await other_tenant.advance_status(f.wamid(2), MessageStatus.READ) is False

    stored = await messages.get_by_provider_id(f.wamid(2))
    assert stored.status == MessageStatus.SENT.value


# --- history_before: what VS-005 sends the model ----------------------------
#
# created_at is set explicitly on every row. PostgreSQL's now() is the
# TRANSACTION start time, so rows written in one test would otherwise share one
# timestamp and the ordering these tests are about would be undefined.


async def _conversation_with_history(session, tenant=f.TENANT_A, texts=("a", "b", "c", "d")):
    """A conversation with `texts` as inbound messages, one minute apart."""
    contact = f.make_contact(tenant)
    session.add(contact)
    await session.flush()
    conversation = f.make_conversation(contact)
    session.add(conversation)
    await session.flush()
    rows = []
    for index, text in enumerate(texts):
        message = f.make_message(conversation, text=text, provider_message_id=f.wamid(index + 1))
        message.created_at = f.LONG_AGO + dt.timedelta(minutes=index)
        session.add(message)
        rows.append(message)
    await session.flush()
    return conversation, rows


async def test_history_is_the_newest_messages_before_the_given_one_oldest_first(db_session):
    """Oldest first, because that is the order a chat model reads."""
    conversation, rows = await _conversation_with_history(
        db_session, texts=("one", "two", "three", "four", "five")
    )
    messages = MessageRepository(db_session, f.TENANT_A)

    history = await messages.history_before(conversation.id, rows[-1].id, limit=3)

    assert [m.text for m in history] == ["two", "three", "four"]


async def test_history_excludes_the_message_being_answered_and_anything_after_it(db_session):
    """Plan assumption A8.

    Two messages a second apart produce two jobs, and the job answering the
    FIRST can find the second already stored. Without the cut, its prompt would
    put the later message before the one it is answering - the model would be
    asked to reply to a question it had already been shown the sequel to.
    """
    conversation, rows = await _conversation_with_history(
        db_session, texts=("first", "answered", "arrived later")
    )
    messages = MessageRepository(db_session, f.TENANT_A)

    history = await messages.history_before(conversation.id, rows[1].id, limit=20)

    assert [m.text for m in history] == ["first"]


async def test_history_skips_failed_outbound_messages(db_session):
    """Requirement 5: a reply that never reached the patient is not something
    the clinic said.

    Every other outbound status stays: QUEUED (reserved, in flight), SENT,
    DELIVERED and READ are all things the patient has been or is about to be
    told. Inbound rows are never filtered - a FAILED inbound cannot happen, and
    if one ever did it would still be something the patient wrote.
    """
    conversation, rows = await _conversation_with_history(db_session, texts=("hello",))
    messages = MessageRepository(db_session, f.TENANT_A)
    statuses = [
        MessageStatus.FAILED,
        MessageStatus.QUEUED,
        MessageStatus.SENT,
        MessageStatus.DELIVERED,
        MessageStatus.READ,
    ]
    for index, status in enumerate(statuses):
        reply = f.make_message(
            conversation,
            direction=MessageDirection.OUTBOUND.value,
            status=status.value,
            text=f"outbound {status.value}",
        )
        reply.created_at = f.LONG_AGO + dt.timedelta(minutes=10 + index)
        db_session.add(reply)
    anchor = f.make_message(conversation, text="the new one", provider_message_id=f.wamid(90))
    anchor.created_at = f.LONG_AGO + dt.timedelta(minutes=30)
    db_session.add(anchor)
    await db_session.flush()

    history = await messages.history_before(conversation.id, anchor.id, limit=20)

    assert [m.text for m in history] == [
        "hello",
        "outbound QUEUED",
        "outbound SENT",
        "outbound DELIVERED",
        "outbound READ",
    ]


async def test_the_limit_counts_only_messages_it_returns(db_session):
    """Why the filter is in SQL and not in Python.

    AGENT_HISTORY_MESSAGES is a promise about what the model SEES. Fetching N
    rows and then dropping the failed ones would make a conversation with a run
    of failed replies arrive at the model with almost no memory at all.
    """
    conversation, _ = await _conversation_with_history(db_session, texts=("keep 1", "keep 2"))
    messages = MessageRepository(db_session, f.TENANT_A)
    for index in range(4):
        failed = f.make_message(
            conversation,
            direction=MessageDirection.OUTBOUND.value,
            status=MessageStatus.FAILED.value,
            text=f"failed {index}",
        )
        failed.created_at = f.LONG_AGO + dt.timedelta(minutes=5 + index)
        db_session.add(failed)
    keeper = f.make_message(conversation, text="keep 3")
    keeper.created_at = f.LONG_AGO + dt.timedelta(minutes=20)
    db_session.add(keeper)
    anchor = f.make_message(conversation, text="the new one", provider_message_id=f.wamid(91))
    anchor.created_at = f.LONG_AGO + dt.timedelta(minutes=30)
    db_session.add(anchor)
    await db_session.flush()

    history = await messages.history_before(conversation.id, anchor.id, limit=3)

    assert [m.text for m in history] == ["keep 1", "keep 2", "keep 3"]


async def test_history_is_tenant_scoped(db_session):
    """Hard rule 4. Another clinic's messages are not context, they are a leak."""
    conversation, rows = await _conversation_with_history(db_session, texts=("mine", "answered"))

    history = await MessageRepository(db_session, f.TENANT_B).history_before(
        conversation.id, rows[1].id, limit=20
    )

    assert history == []


async def test_history_is_scoped_to_one_conversation(db_session):
    """A new conversation after a CLOSED one starts with no memory of it.

    That is deliberate: CLOSED means the clinic considered the thread finished,
    and carrying it forward would have the AI answer a new question from an old
    context.
    """
    contact = f.make_contact(f.TENANT_A)
    db_session.add(contact)
    await db_session.flush()
    closed = f.make_conversation(contact, state=ConversationState.CLOSED.value)
    db_session.add(closed)
    current = f.make_conversation(contact)
    db_session.add(current)
    await db_session.flush()
    old = f.make_message(closed, text="from the closed thread")
    old.created_at = f.LONG_AGO
    db_session.add(old)
    anchor = f.make_message(current, text="the new one", provider_message_id=f.wamid(92))
    anchor.created_at = f.LONG_AGO + dt.timedelta(minutes=5)
    db_session.add(anchor)
    await db_session.flush()

    history = await MessageRepository(db_session, f.TENANT_A).history_before(
        current.id, anchor.id, limit=20
    )

    assert history == []


async def test_a_limit_of_zero_returns_nothing_without_a_query(db_session):
    """AGENT_HISTORY_MESSAGES=0 is a supported setting: reply to each message
    on its own. LIMIT 0 would work; a negative limit is a SQL error, and both
    are answered here before any statement is built."""
    conversation, rows = await _conversation_with_history(db_session, texts=("a", "b"))
    messages = MessageRepository(db_session, f.TENANT_A)

    assert await messages.history_before(conversation.id, rows[1].id, limit=0) == []
    assert await messages.history_before(conversation.id, rows[1].id, limit=-1) == []
