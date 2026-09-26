"""Repositories. The database enforces the rules; these make them convenient
and make tenant_id impossible to forget."""

import datetime as dt
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
from app.db.models import ContactIdentity, Conversation
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

    assert f.phone(7) in str(raised.value)  # the raw error does leak it
    translated = as_duplicate(raised.value)
    assert isinstance(translated, DuplicateRecordError)
    assert f.phone(7) not in str(translated)
    assert f.phone(7) not in repr(translated)
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
        tenant_id=uuid.uuid4(),
    )
    assert with_tenant.tenant_id is not None
