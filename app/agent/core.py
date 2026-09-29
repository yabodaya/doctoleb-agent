"""The Agent Core: one turn in, one reply out.

docs/architecture.md's `process_turn`. It knows nothing about WhatsApp and
touches no database - by construction, not by discipline: this package imports
no session, no repository, no model, no SDK, no HTTP stack, no settings and no
concrete booking client, and tests/agent/test_process_turn.py asserts it
(hard rule 3, plan conflict C2).
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.agent.clock import Clock, clock_message
from app.agent.history import HistoryEntry, content_for, to_chat_messages
from app.agent.loop import LoopState, run_loop
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.agent.tools import ToolCallRecord, ToolContext, ToolCrashed, default_registry
from app.agent.tools.registry import ToolRegistry
from app.db.enums import MessageModality
from app.integrations.booking import BookingClient
from app.integrations.openai import ChatClient, ChatMessage, ChatOutcome

# app.tenants.ids, not app.tenants: the package's __init__ re-exports nothing
# precisely so this import cannot reach the resolver, and therefore app.config.
from app.tenants.ids import TenantId


@dataclass(frozen=True)
class Turn:
    """One inbound message, with everything needed to answer it.

    The five fields docs/architecture.md documents, plus `history` (plan
    conflict C2). The history arrives as plain data, loaded by the caller in a
    transaction it has already committed and CLOSED - which is what lets the
    model call run with no transaction open, so a staff takeover never waits
    for OpenAI.

    tenant_id and contact_id are NEVER sent to the model (hard rule 4):
    build_messages does not read them. The tenant reaches the tools through
    ToolContext instead, where the model cannot see or change it.
    """

    tenant_id: TenantId
    contact_id: uuid.UUID
    conversation_id: uuid.UUID
    modality: MessageModality
    input_text: str | None = field(default=None, repr=False)
    history: tuple[HistoryEntry, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class AgentRuntime:
    """What one turn is allowed to use, injected by the job (plan conflict C7).

    `docs/architecture.md` documented `process_turn(turn, chat)`; VS-006 needs a
    booking client, a clock and a budget, and bundling them keeps the signature
    from growing a parameter per slice.

    `registry` has a default so tests and the job get the same three tools
    without either naming them.
    """

    booking: BookingClient
    clock: Clock
    turn_timeout_seconds: float
    registry: ToolRegistry = field(default_factory=default_registry)


@dataclass(frozen=True)
class AgentResult:
    """What the agent produced, and how it went.

    `reply_text` is set only for SUCCESS. `prompt_version` is logged with every
    generation (plan assumption A6), so a change in the AI's behaviour can be
    matched to a change in its instructions.

    `tool_calls` are plain-data records the JOB persists in T1b, together with
    the reply reservation. They are returned rather than written here because
    this package cannot open a transaction - which is the whole point of it not
    being able to import one.
    """

    outcome: ChatOutcome
    reason: str
    reply_text: str | None = field(default=None, repr=False)
    prompt_version: str = SYSTEM_PROMPT_VERSION
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model_calls: int = 0
    tool_calls: tuple[ToolCallRecord, ...] = ()


def build_messages(turn: Turn, now: datetime) -> list[ChatMessage]:
    """System prompt, history, the clock, then the message being answered.

    The clock message goes LATE, not next to the prompt, for two reasons: it
    keeps the static prefix (prompt, tool schemas, history) identical from one
    turn to the next, which is what OpenAI's automatic prompt caching needs; and
    it puts the date right next to the question that needs it.

    The answered message goes through the same content_for() as the history, so
    a voice note is described identically whether it is being answered or
    remembered. Nothing else about the patient is sent: no name, no phone
    number, no ids, no timestamps (hard rules 4 and 8).
    """
    return [
        ChatMessage("system", SYSTEM_PROMPT),
        *to_chat_messages(turn.history),
        clock_message(now),
        ChatMessage("user", content_for(turn.modality, turn.input_text)),
    ]


async def process_turn(turn: Turn, chat: ChatClient, runtime: AgentRuntime) -> AgentResult:
    """docs/architecture.md's Agent Core entry point, now with tools.

    ONE `asyncio.timeout` wraps the whole loop: up to MAX_MODEL_CALLS model
    calls and every tool call between them. Nothing in this package logs - the
    job writes one line per generation, because only the job knows the
    webhook_inbox row id (plan assumption A5).
    """
    now = runtime.clock()
    ctx = ToolContext(turn.tenant_id, runtime.booking, now)
    state = LoopState()

    def result(outcome: ChatOutcome, reason: str, reply_text: str | None = None) -> AgentResult:
        return AgentResult(
            outcome=outcome,
            reason=reason,
            reply_text=reply_text,
            prompt_version=SYSTEM_PROMPT_VERSION,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            model_calls=state.model_calls,
            tool_calls=tuple(state.records),
        )

    try:
        async with asyncio.timeout(runtime.turn_timeout_seconds) as deadline:
            outcome, reason, reply_text = await run_loop(
                build_messages(turn, now), chat, runtime.registry, ctx, state
            )
        return result(outcome, reason, reply_text)
    except TimeoutError:
        if not deadline.expired():
            # Not our deadline: somebody else's TimeoutError passing through,
            # which means a bug. Letting it escape is right - swallowing it
            # would report a bug as "OpenAI was slow" and retry it five times.
            raise
        # The tool that was running at that moment gets a record, so a gap in
        # `sequence` never goes unexplained.
        state.close_in_flight("turn_timeout")
        # RETRYABLE (Q3), consistent with VS-005's openai_timeout: retried with
        # backoff, fallback plus dead letter on the last try.
        return result(ChatOutcome.RETRYABLE, "agent_turn_timeout")
    except ToolCrashed as crash:
        # PERMANENT (Q6): a bug in our code is not something a retry fixes, and
        # hard rule 11 wants a dead letter rather than a stranded job.
        state.record_crash(crash)
        return result(ChatOutcome.PERMANENT, "agent_tool_crashed")
