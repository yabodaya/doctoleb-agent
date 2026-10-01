"""The Agent Core entry point: what the model is shown, and what comes back."""

import ast
import pathlib
import uuid
from datetime import UTC, datetime

import pytest

from app.agent import (
    AgentResult,
    AgentRuntime,
    HistoryEntry,
    Turn,
    build_messages,
    process_turn,
)
from app.agent.history import VOICE_NOTE_PLACEHOLDER
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.db.enums import MessageDirection, MessageModality
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.openai import ChatOutcome
from tests.integrations.fakes import AI_REPLY, FakeChatClient, ok, permanent, retryable

PATIENT_TEXT = "can I come in this week?"
NOW = datetime(2026, 9, 29, 7, tzinfo=UTC)  # Tuesday 29 Sep 2026, 10:00 local


def runtime() -> AgentRuntime:
    """VS-006 gave process_turn a runtime (plan conflict C7): a booking client,
    a clock and the turn budget. Frozen, so nothing here depends on today."""
    return AgentRuntime(
        booking=FakeBookingClient.demo(clock=lambda: NOW),
        clock=lambda: NOW,
        turn_timeout_seconds=45.0,
    )


def turn(
    *,
    input_text: str | None = PATIENT_TEXT,
    modality: MessageModality = MessageModality.TEXT,
    history: tuple[HistoryEntry, ...] = (),
    patient_reference: str | None = None,
) -> Turn:
    return Turn(
        tenant_id="clinic-alpha",  # opaque, never a UUID (decision D1)
        contact_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        modality=modality,
        input_text=input_text,
        history=history,
        # VS-007: the patient's phone number, and therefore something no repr may
        # show. Left None by default, so every VS-005 and VS-006 test here builds
        # exactly the turn it did before.
        patient_reference=patient_reference,
    )


def test_the_first_message_is_the_system_prompt():
    messages = build_messages(turn(), NOW)

    assert messages[0].role == "system"
    assert messages[0].content == SYSTEM_PROMPT


def test_the_history_comes_next_then_the_message_being_answered():
    history = (
        HistoryEntry(MessageDirection.INBOUND, MessageModality.TEXT, "hello"),
        HistoryEntry(MessageDirection.OUTBOUND, MessageModality.TEXT, "hi, how can we help?"),
    )

    messages = build_messages(turn(history=history), NOW)

    # The clock message sits between the history and the answered message
    # (decision D3). VS-005 asserted this list without it; the change is
    # deliberate and tests/agent/test_tool_loop.py explains the placement.
    assert [(m.role, m.content) for m in messages[1:3]] == [
        ("user", "hello"),
        ("assistant", "hi, how can we help?"),
    ]
    assert messages[3].role == "system"
    assert "Current date and time at the clinic" in messages[3].content
    assert (messages[4].role, messages[4].content) == ("user", PATIENT_TEXT)
    assert len(messages) == 5


def test_a_voice_note_being_answered_is_sent_as_its_placeholder():
    """The message being answered goes through the same content_for() as the
    history, so the two can never describe one kind of message differently."""
    messages = build_messages(turn(input_text=None, modality=MessageModality.VOICE_NOTE), NOW)

    assert messages[-1].role == "user"
    assert messages[-1].content == VOICE_NOTE_PLACEHOLDER


async def test_a_successful_turn_returns_the_text_the_prompt_version_and_the_token_counts():
    chat = FakeChatClient(ok())

    result = await process_turn(turn(), chat, runtime())

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.reason == "ok"
    assert result.reply_text == AI_REPLY
    assert result.prompt_version == SYSTEM_PROMPT_VERSION
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7
    assert len(chat.calls) == 1


@pytest.mark.parametrize(
    ("result", "outcome", "reason"),
    [
        pytest.param(retryable(), ChatOutcome.RETRYABLE, "openai_http_503", id="retryable"),
        pytest.param(
            permanent(), ChatOutcome.PERMANENT, "openai_insufficient_quota", id="permanent"
        ),
    ],
)
async def test_a_failed_turn_carries_the_outcome_and_reason_and_no_text(result, outcome, reason):
    generated = await process_turn(turn(), FakeChatClient(result), runtime())

    assert generated.outcome is outcome
    assert generated.reason == reason
    assert generated.reply_text is None


def test_the_agent_imports_neither_the_sdk_nor_the_database():
    """Hard rule 3 made structural, and plan conflict C2 made enforceable.

    process_turn CANNOT hold a transaction open across the model call, because
    it has no way to open one: the package imports no session, no repository
    and no model. app.db.enums is allowed - it is a vocabulary, not access -
    and so is the ChatClient interface, which is the whole point of having one.

    VS-006 widened the list (plan section 5.7). The additions and why:

    - httpx / httpx2 / asyncpg / redis: the loop must not be able to open a
      connection of any kind. The booking client arrives as a Protocol.
    - app.integrations.booking.fake: demo data is the WORKER's choice, made
      visibly at startup, never something the Agent Core reaches for.
    - app.integrations.booking.memory (VS-007, V8): the same reasoning, and one
      more. That module is STATEFUL. If the Agent Core could import it, "where
      does a hold live" would have a second answer, and the tool loop could
      reach a booking backend without going through the injected Protocol.
    - app.config: a turn's budget arrives on AgentRuntime. If the agent could
      read Settings, "how long may a turn take" would have two answers.
    - app.worker / arq: the job depends on the agent, never the other way.

    NOTE the limit of this test: it sees DIRECT imports only. A module that
    imported something innocent which itself imported app.config would pass -
    which is exactly what happened with app.tenants (see
    tests/db/test_tenant_text.py, and amendment B1).
    """
    forbidden = (
        "openai",
        "sqlalchemy",
        "app.db.repositories",
        "app.db.session",
        "app.db.models",
        "app.channels",
        "httpx",
        "httpx2",
        "asyncpg",
        "redis",
        "arq",
        "app.config",
        "app.worker",
        "app.integrations.booking.fake",
        "app.integrations.booking.memory",
        "app.integrations.openai.chat",
    )
    offenders: list[str] = []
    for path in pathlib.Path("app/agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == bad or name.startswith(bad + ".") for bad in forbidden):
                    offenders.append(f"{path.as_posix()}: {name}")

    assert offenders == []


def test_the_agent_never_logs():
    """VS-005 A5. One line per generation, written by the JOB.

    Only the job knows the webhook_inbox row id that every other line carries,
    so a line written here would be an orphan - and the agent handles
    model-written text, which is the last thing that should reach a logger by
    accident.
    """
    offenders: list[str] = []
    for path in pathlib.Path("app/agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if "logging" in names:
                    offenders.append(f"{path.as_posix()}: imports logging")
            if isinstance(node, ast.Call) and "logger" in ast.unparse(node.func):
                offenders.append(f"{path.as_posix()}:{node.lineno}")

    assert offenders == []


def test_no_repr_shows_message_content():
    """pytest prints reprs on a failed assertion. Everything that carries
    patient or generated text keeps it out of its repr (hard rule 8)."""
    from app.agent import ToolCallRecord, ToolContext, ToolExecutionStatus
    from app.integrations.openai import ChatMessage, ToolCallRequest

    sentinel = "REPR-SENTINEL-patient-words"
    entry = HistoryEntry(MessageDirection.INBOUND, MessageModality.TEXT, sentinel)
    call = ToolCallRequest("call_1", f"{sentinel}_tool", f'{{"note":"{sentinel}"}}')

    assert sentinel not in repr(entry)
    assert sentinel not in repr(turn(input_text=sentinel, history=(entry,)))
    assert sentinel not in repr(
        AgentResult(ChatOutcome.SUCCESS, "ok", sentinel, SYSTEM_PROMPT_VERSION, 1, 1)
    )
    # VS-006's additions. A tool call's name AND arguments are model-written,
    # and an AgentResult now carries the records too.
    assert sentinel not in repr(call)
    assert sentinel not in repr(ChatMessage("assistant", sentinel, tool_calls=(call,)))
    assert sentinel not in repr(ChatMessage("tool", sentinel, tool_call_id="call_1"))
    assert sentinel not in repr(
        AgentResult(
            ChatOutcome.SUCCESS,
            "ok",
            sentinel,
            SYSTEM_PROMPT_VERSION,
            1,
            1,
            3,
            (ToolCallRecord(0, 1, "list_doctors", (), ToolExecutionStatus.OK, None, 4),),
        )
    )
    # ToolContext is deliberately NOT in this list: its repr shows the tenant,
    # which is a clinic identifier rather than patient content, and showing it
    # is what makes a traceback say which clinic a failure belonged to.
    # tests/agent/test_tools.py pins that it shows nothing else.
    assert repr(ToolContext("clinic-alpha", FakeBookingClient.demo(clock=lambda: NOW), NOW)) == (
        "ToolContext(tenant_id='clinic-alpha')"
    )

    # VS-007's additions, extending the same rule to the booking objects. Three
    # new kinds of thing must stay out of a repr: the patient's PHONE NUMBER (the
    # patient reference the Booking Service asked for), the Booking Service's own
    # ids and our idempotency key, and the RECEIPT - which is a doctor's name and
    # an appointment time (plan section 5.13).
    from app.agent import (
        BookingOutcome,
        BookingState,
        ChangePhase,
        ChangeStatus,
        PatientContext,
    )
    from app.db.enums import BookingActionKind, BookingActionStatus
    from app.integrations.booking import PatientRef

    state = BookingState(
        action_id=uuid.uuid4(),
        kind=BookingActionKind.BOOK,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id=sentinel,
        appointment_id=sentinel,
    )
    outcome = BookingOutcome(
        kind=BookingActionKind.BOOK,
        phase=ChangePhase.EXECUTED,
        status=ChangeStatus.SUCCESS,
        hold_id=sentinel,
        appointment_id=sentinel,
        idempotency_key=sentinel,
        receipt=sentinel,
    )
    patient = PatientContext(
        bookings=FakeBookingClient.demo(clock=lambda: NOW),
        patient=PatientRef(sentinel),
        inbox_event_id=uuid.uuid4(),
        inbound_message_id=uuid.uuid4(),
        state=state,
    )

    assert sentinel not in repr(state)
    assert sentinel not in repr(outcome)
    assert sentinel not in repr(patient)
    assert sentinel not in repr(PatientRef(sentinel))
    assert sentinel not in repr(turn(input_text=sentinel, patient_reference=sentinel))
    assert sentinel not in repr(
        AgentResult(
            ChatOutcome.SUCCESS,
            "ok",
            sentinel,
            SYSTEM_PROMPT_VERSION,
            1,
            1,
            3,
            (
                ToolCallRecord(
                    0, 1, "book_appointment", ("full_name",), ToolExecutionStatus.OK, None, 4
                ),
            ),
            outcome,
        )
    )
