"""The Agent Core: one turn in, one reply out.

docs/architecture.md's `process_turn`. It knows nothing about WhatsApp and
touches no database - by construction, not by discipline: this package imports
no session, no repository and no model, and tests/agent/test_process_turn.py
asserts it (hard rule 3, plan conflict C2).
"""

import uuid
from dataclasses import dataclass, field

from app.agent.history import HistoryEntry, content_for, to_chat_messages
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.db.enums import MessageModality
from app.integrations.openai import ChatClient, ChatMessage, ChatOutcome

# app.tenants.ids, not app.tenants: the package's __init__ imports the resolver,
# which reads Settings, and app/agent/ must not import app.config.
from app.tenants.ids import TenantId


@dataclass(frozen=True)
class Turn:
    """One inbound message, with everything needed to answer it.

    The five fields docs/architecture.md documents, plus `history` (plan
    conflict C2). The history arrives as plain data, loaded by the caller in a
    transaction it has already committed and CLOSED - which is what lets the
    model call run with no transaction open, so a staff takeover never waits
    for OpenAI.

    tenant_id and contact_id are carried for VS-006's tools and are NEVER sent
    to the model (hard rule 4): build_messages does not read them.
    """

    tenant_id: TenantId
    contact_id: uuid.UUID
    conversation_id: uuid.UUID
    modality: MessageModality
    input_text: str | None = field(default=None, repr=False)
    history: tuple[HistoryEntry, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class AgentResult:
    """What the agent produced, and how it went.

    `reply_text` is set only for SUCCESS. `prompt_version` is logged with every
    generation (plan assumption A6), so a change in the AI's behaviour can be
    matched to a change in its instructions. Tool calls arrive in VS-006 and the
    handoff flag in VS-010, when something can populate them.
    """

    outcome: ChatOutcome
    reason: str
    reply_text: str | None = field(default=None, repr=False)
    prompt_version: str = SYSTEM_PROMPT_VERSION
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def build_messages(turn: Turn) -> list[ChatMessage]:
    """The system prompt, the earlier messages, then the one being answered.

    The answered message goes through the same content_for() as the history, so
    a voice note is described identically whether it is being answered or
    remembered. Nothing else about the patient is sent: no name, no phone
    number, no ids, no timestamps (hard rules 4 and 8).
    """
    return [
        ChatMessage("system", SYSTEM_PROMPT),
        *to_chat_messages(turn.history),
        ChatMessage("user", content_for(turn.modality, turn.input_text)),
    ]


async def process_turn(turn: Turn, chat: ChatClient) -> AgentResult:
    """docs/architecture.md's Agent Core entry point. VS-005: no tools yet.

    One model call per turn, and never a retry: the ChatClient makes one
    attempt and classifies it, the job decides what to do with the answer.
    Nothing in this package logs - the job writes one line per generation,
    because only the job knows the webhook_inbox row id (plan assumption A5).
    """
    result = await chat.complete(build_messages(turn))
    return AgentResult(
        outcome=result.outcome,
        reason=result.reason,
        reply_text=result.text,
        prompt_version=SYSTEM_PROMPT_VERSION,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
