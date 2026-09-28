"""The Agent Core.

No database, no HTTP, no WhatsApp: the job loads what a turn needs, commits and
closes its transaction, and calls process_turn with plain data (hard rule 3,
plan conflict C2).
"""

from app.agent.core import AgentResult, Turn, build_messages, process_turn
from app.agent.history import (
    NON_TEXT_PLACEHOLDER,
    VOICE_NOTE_PLACEHOLDER,
    HistoryEntry,
    content_for,
    to_chat_messages,
)
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION

__all__ = [
    "NON_TEXT_PLACEHOLDER",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_VERSION",
    "VOICE_NOTE_PLACEHOLDER",
    "AgentResult",
    "HistoryEntry",
    "Turn",
    "build_messages",
    "content_for",
    "process_turn",
    "to_chat_messages",
]
