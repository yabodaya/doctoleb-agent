"""Synthetic test data.

Hard rule 8 forbids test fixtures built from real patient data. Everything here
is generated from an integer, so nothing in this repo's history can ever be
traced to a person.
"""

import datetime as dt
import uuid
from typing import Any

from app.db.enums import (
    AgentRunOutcome,
    BookingActionKind,
    BookingActionStatus,
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
    ToolExecutionStatus,
    VoiceNoteStatus,
)
from app.db.models import (
    AgentRun,
    BookingAction,
    Contact,
    ContactIdentity,
    Conversation,
    Message,
    ToolExecution,
    VoiceNote,
    WebhookInbox,
)
from app.tenants.ids import TenantId

# Deliberately NOT UUIDs (decision D1): a tenant id is an opaque string, and
# test data that looks like a UUID would let a quiet uuid.UUID(...) parse
# survive anywhere in the stack.
TENANT_A: TenantId = "clinic-alpha"
TENANT_B: TenantId = "clinic-beta"

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


def make_contact(tenant_id: TenantId = TENANT_A, **overrides: Any) -> Contact:
    values: dict[str, Any] = {"tenant_id": tenant_id, "display_name": "Test Patient"}
    values.update(overrides)
    return Contact(**values)


def make_identity(
    contact: Contact, n: int = 1, tenant_id: TenantId | None = None, **overrides: Any
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


def make_agent_run(conversation: Conversation, inbound: Message, **overrides: Any) -> AgentRun:
    """A recorded turn. Codes, counts and ids only - there is nothing else to
    put here, which is the point of the table."""
    values: dict[str, Any] = {
        "tenant_id": conversation.tenant_id,
        "inbox_event_id": uuid.uuid4(),
        "conversation_id": conversation.id,
        "inbound_message_id": inbound.id,
        "job_try": 1,
        "model": "test-model",
        "prompt_version": "vs006-1",
        "outcome": AgentRunOutcome.SUCCESS.value,
        "reason": "ok",
        "model_calls": 1,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "duration_ms": 123,
    }
    values.update(overrides)
    return AgentRun(**values)


def make_tool_execution(run: AgentRun, sequence: int = 0, **overrides: Any) -> ToolExecution:
    values: dict[str, Any] = {
        "agent_run_id": run.id,
        "tenant_id": run.tenant_id,
        "sequence": sequence,
        "model_call": 1,
        "tool_name": "list_doctors",
        "argument_names": [],
        "status": ToolExecutionStatus.OK.value,
        "error_code": None,
        "duration_ms": 4,
    }
    values.update(overrides)
    return ToolExecution(**values)


def make_voice_note(message: Message, **overrides: Any) -> VoiceNote:
    """One voice note's bookkeeping, PENDING by default.

    Ids, codes and counts - there is nothing else this table may hold, which is
    why the factory takes no text at all. Not even a transcript argument exists:
    the transcript belongs on `messages.text`, so a test that wants one sets it
    there (hard rule 8).
    """
    values: dict[str, Any] = {
        "tenant_id": message.tenant_id,
        "message_id": message.id,
        "inbox_event_id": uuid.uuid4(),
        "media_id": "media-id-0000001",
        "mime_type": "audio/ogg",
        "voice": True,
        "status": VoiceNoteStatus.PENDING.value,
        "attempts": 1,
    }
    values.update(overrides)
    return VoiceNote(**values)


def make_booking_action(
    conversation: Conversation, inbound: Message, **overrides: Any
) -> BookingAction:
    """A PENDING hold, prepared while answering `inbound`.

    Ids, codes and one operational expiry - there is nothing else this table may
    hold (hard rule 8), which is why the factory takes no text at all.

    `hold_expires_at` is left to the caller, because the two clocks matter here: a
    test that wants the hold to lapse sets it against the INJECTED clock it also
    gives the service, never against PostgreSQL's now() (plan risk R5).
    """
    values: dict[str, Any] = {
        "tenant_id": conversation.tenant_id,
        "conversation_id": conversation.id,
        "kind": BookingActionKind.BOOK.value,
        "status": BookingActionStatus.PENDING.value,
        "hold_id": "hold_1",
        "created_by_inbox_event_id": uuid.uuid4(),
        "created_by_inbound_message_id": inbound.id,
    }
    values.update(overrides)
    return BookingAction(**values)
