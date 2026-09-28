"""The Agent Core entry point: what the model is shown, and what comes back."""

import ast
import pathlib
import uuid

import pytest

from app.agent import AgentResult, HistoryEntry, Turn, build_messages, process_turn
from app.agent.history import VOICE_NOTE_PLACEHOLDER
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.db.enums import MessageDirection, MessageModality
from app.integrations.openai import ChatOutcome
from tests.integrations.fakes import AI_REPLY, FakeChatClient, ok, permanent, retryable

PATIENT_TEXT = "can I come in this week?"


def turn(
    *,
    input_text: str | None = PATIENT_TEXT,
    modality: MessageModality = MessageModality.TEXT,
    history: tuple[HistoryEntry, ...] = (),
) -> Turn:
    return Turn(
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        modality=modality,
        input_text=input_text,
        history=history,
    )


def test_the_first_message_is_the_system_prompt():
    messages = build_messages(turn())

    assert messages[0].role == "system"
    assert messages[0].content == SYSTEM_PROMPT


def test_the_history_comes_next_then_the_message_being_answered():
    history = (
        HistoryEntry(MessageDirection.INBOUND, MessageModality.TEXT, "hello"),
        HistoryEntry(MessageDirection.OUTBOUND, MessageModality.TEXT, "hi, how can we help?"),
    )

    messages = build_messages(turn(history=history))

    assert [(m.role, m.content) for m in messages[1:]] == [
        ("user", "hello"),
        ("assistant", "hi, how can we help?"),
        ("user", PATIENT_TEXT),
    ]


def test_a_voice_note_being_answered_is_sent_as_its_placeholder():
    """The message being answered goes through the same content_for() as the
    history, so the two can never describe one kind of message differently."""
    messages = build_messages(turn(input_text=None, modality=MessageModality.VOICE_NOTE))

    assert messages[-1].role == "user"
    assert messages[-1].content == VOICE_NOTE_PLACEHOLDER


async def test_a_successful_turn_returns_the_text_the_prompt_version_and_the_token_counts():
    chat = FakeChatClient(ok())

    result = await process_turn(turn(), chat)

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
    generated = await process_turn(turn(), FakeChatClient(result))

    assert generated.outcome is outcome
    assert generated.reason == reason
    assert generated.reply_text is None


def test_the_agent_imports_neither_the_sdk_nor_the_database():
    """Hard rule 3 made structural, and plan conflict C2 made enforceable.

    process_turn CANNOT hold a transaction open across the model call, because
    it has no way to open one: the package imports no session, no repository
    and no model. app.db.enums is allowed - it is a vocabulary, not access -
    and so is the ChatClient interface, which is the whole point of having one.
    """
    forbidden = (
        "openai",
        "sqlalchemy",
        "app.db.repositories",
        "app.db.session",
        "app.db.models",
        "app.channels",
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


def test_no_repr_shows_message_content():
    """pytest prints reprs on a failed assertion. Everything that carries
    patient or generated text keeps it out of its repr (hard rule 8)."""
    sentinel = "REPR-SENTINEL-patient-words"
    entry = HistoryEntry(MessageDirection.INBOUND, MessageModality.TEXT, sentinel)

    assert sentinel not in repr(entry)
    assert sentinel not in repr(turn(input_text=sentinel, history=(entry,)))
    assert sentinel not in repr(
        AgentResult(ChatOutcome.SUCCESS, "ok", sentinel, SYSTEM_PROMPT_VERSION, 1, 1)
    )
