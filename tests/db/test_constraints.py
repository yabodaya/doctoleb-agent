"""What the database refuses to store. These are the safety net, not the
repositories on top of them."""

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import ConversationState
from app.db.models import Contact, Conversation, Message, WebhookInbox
from tests.db import factories as f

pytestmark = pytest.mark.db


async def test_duplicate_provider_event_id_is_rejected(db_session):
    # Hard rule 2: the same Meta event delivered twice produces exactly one
    # stored message and one reply.
    db_session.add(f.make_inbox(1))
    await db_session.flush()
    db_session.add(f.make_inbox(1))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_second_distinct_event_is_stored(db_session):
    db_session.add_all([f.make_inbox(1), f.make_inbox(2)])
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(WebhookInbox))
    assert count == 2


async def test_webhook_inbox_accepts_an_unresolved_tenant(db_session):
    # Review Focus 7. The webhook stores before anything resolves a tenant
    # (hard rule 1). A NOT NULL here would force resolution inside the request.
    db_session.add(f.make_inbox(1, tenant_id=None))
    await db_session.flush()

    db_session.add(Contact(tenant_id=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_invalid_conversation_state_is_rejected(db_session):
    # Review Focus 5. Alembic's autogenerate never compares CHECK constraints,
    # so the drift test cannot protect them — this is the only thing standing
    # between a typo'd or silently-widened enum and rows the app cannot read
    # back. Inserted through Core so the ORM does not coerce the value first.
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            sa.insert(Conversation).values(
                id=f.uuid.uuid4(),
                tenant_id=contact.tenant_id,
                contact_id=contact.id,
                channel="whatsapp",
                state="BANANA",
            )
        )


async def test_duplicate_provider_message_id_is_rejected(db_session):
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(f.make_message(conversation, provider_message_id=f.wamid(1)))
    await db_session.flush()
    db_session.add(f.make_message(conversation, provider_message_id=f.wamid(1)))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_many_messages_may_have_no_provider_message_id(db_session):
    # An outbound message that failed before Meta accepted it never gets an id.
    # PostgreSQL permits many NULLs under a unique constraint; if that were not
    # true, the second failed send would crash the worker.
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()

    db_session.add_all(
        [
            f.make_message(conversation, provider_message_id=None),
            f.make_message(conversation, provider_message_id=None),
        ]
    )
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(Message))
    assert count == 2


async def test_the_same_phone_number_may_exist_in_two_tenants(db_session):
    # A patient can be a patient of two clinics. "Unique per tenant" is the
    # whole point of the constraint's shape.
    for tenant in (f.TENANT_A, f.TENANT_B):
        contact = f.make_contact(tenant)
        db_session.add(contact)
        await db_session.flush()
        db_session.add(f.make_identity(contact, n=1))
    await db_session.flush()


async def test_the_same_phone_number_twice_in_one_tenant_is_rejected(db_session):
    first = f.make_contact(f.TENANT_A)
    second = f.make_contact(f.TENANT_A)
    db_session.add_all([first, second])
    await db_session.flush()

    db_session.add(f.make_identity(first, n=1))
    await db_session.flush()
    db_session.add(f.make_identity(second, n=1))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_concurrent_inserts_produce_one_open_conversation(
    db_engine, second_session_factory
):
    # Review Focus 3. Two workers processing two messages from the same patient
    # at the same moment both see no open conversation. This test uses two real
    # connections, because the rollback-wrapped db_session cannot show one
    # session what the other has not committed.
    async with second_session_factory() as setup:
        contact = f.make_contact()
        setup.add(contact)
        await setup.commit()
        contact_id = contact.id

    try:
        async with second_session_factory() as one, second_session_factory() as two:
            one.add(f.make_conversation(contact))
            await one.commit()

            two.add(f.make_conversation(contact))
            with pytest.raises(IntegrityError):
                await two.commit()

        async with second_session_factory() as check:
            count = await check.scalar(
                sa.select(sa.func.count())
                .select_from(Conversation)
                .where(Conversation.contact_id == contact_id)
            )
            assert count == 1
    finally:
        async with second_session_factory() as cleanup:
            await cleanup.execute(sa.delete(Contact).where(Contact.id == contact_id))
            await cleanup.commit()


async def test_closing_a_conversation_frees_the_slot(db_session):
    # The index is partial. Without WHERE state <> 'CLOSED', a patient who ever
    # had a conversation closed could never start another one.
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()

    first = f.make_conversation(contact)
    db_session.add(first)
    await db_session.flush()

    first.state = ConversationState.CLOSED.value
    await db_session.flush()

    db_session.add(f.make_conversation(contact))
    await db_session.flush()


async def test_deleting_a_conversation_deletes_its_messages(db_session):
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(f.make_message(conversation))
    await db_session.flush()

    await db_session.execute(sa.delete(Conversation).where(Conversation.id == conversation.id))
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(Message))
    assert count == 0


async def test_stored_timestamps_come_back_timezone_aware(db_session):
    # A naive TIMESTAMP column returns a datetime with tzinfo=None, and every
    # later comparison against an aware "now" raises TypeError — or worse,
    # silently computes the 24h WhatsApp window in the wrong zone.
    row = f.make_inbox(1)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.created_at.tzinfo is not None
    assert row.created_at.utcoffset() is not None
    assert abs(row.created_at - dt.datetime.now(dt.UTC)) < dt.timedelta(minutes=5)
