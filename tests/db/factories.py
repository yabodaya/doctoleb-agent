"""Synthetic test data.

Hard rule 8 forbids test fixtures built from real patient data. Everything here
is generated from an integer, so nothing in this repo's history can ever be
traced to a person.
"""

import datetime as dt
import uuid
from typing import Any

from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Contact, ContactIdentity, Conversation, Message, WebhookInbox

TENANT_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")

# PostgreSQL's now() is the TRANSACTION start time, constant for a whole test.
# Any assertion that a timestamp column moved needs the column set to a fixed
# past value first, or it compares now() against now() and can never fail.
LONG_AGO = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)


def phone(n: int) -> str:
    """A synthetic MSISDN in a documentation-safe range."""
    return f"96170{n:06d}"


def event_id(n: int) -> str:
    return f"evt-{n:08d}"


def wamid(n: int) -> str:
    return f"wamid.TEST{n:08d}"


def make_inbox(n: int, **overrides: Any) -> WebhookInbox:
    values: dict[str, Any] = {
        "provider": Channel.WHATSAPP.value,
        "provider_event_id": event_id(n),
        "payload": {"n": n},
        "status": InboxStatus.RECEIVED.value,
    }
    values.update(overrides)
    return WebhookInbox(**values)


def make_contact(tenant_id: uuid.UUID = TENANT_A, **overrides: Any) -> Contact:
    values: dict[str, Any] = {"tenant_id": tenant_id, "display_name": "Test Patient"}
    values.update(overrides)
    return Contact(**values)


def make_identity(
    contact: Contact, n: int = 1, tenant_id: uuid.UUID | None = None, **overrides: Any
) -> ContactIdentity:
    values: dict[str, Any] = {
        "tenant_id": tenant_id or contact.tenant_id,
        "contact_id": contact.id,
        "channel": Channel.WHATSAPP.value,
        "external_id": phone(n),
    }
    values.update(overrides)
    return ContactIdentity(**values)


def make_conversation(contact: Contact, **overrides: Any) -> Conversation:
    values: dict[str, Any] = {
        "tenant_id": contact.tenant_id,
        "contact_id": contact.id,
        "channel": Channel.WHATSAPP.value,
        "state": ConversationState.AI_ACTIVE.value,
    }
    values.update(overrides)
    return Conversation(**values)


def make_message(conversation: Conversation, **overrides: Any) -> Message:
    values: dict[str, Any] = {
        "tenant_id": conversation.tenant_id,
        "conversation_id": conversation.id,
        "direction": MessageDirection.INBOUND.value,
        "modality": MessageModality.TEXT.value,
        "status": MessageStatus.RECEIVED.value,
        "text": "synthetic message body",
    }
    values.update(overrides)
    return Message(**values)


def make_reply(conversation: Conversation, inbound: Message, **overrides: Any) -> Message:
    """An outbound reply linked to the inbound message it answers.

    QUEUED with no provider_message_id: the state a reply row is reserved in,
    before Meta has been asked anything (VS-004's commit boundary T1).
    """
    values: dict[str, Any] = {
        "tenant_id": conversation.tenant_id,
        "conversation_id": conversation.id,
        "direction": MessageDirection.OUTBOUND.value,
        "modality": MessageModality.TEXT.value,
        "status": MessageStatus.QUEUED.value,
        "text": "Received",
        "reply_to_message_id": inbound.id,
    }
    values.update(overrides)
    return Message(**values)
