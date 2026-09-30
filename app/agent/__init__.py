"""The Agent Core.

No database, no HTTP, no WhatsApp: the job loads what a turn needs, commits and
closes its transaction, and calls process_turn with plain data (hard rule 3,
plan conflict C2).
"""

from app.agent.clock import CLINIC_TIMEZONE, CLINIC_TZ, Clock, clock_message, utc_now
from app.agent.core import (
    AgentResult,
    AgentRuntime,
    Turn,
    build_messages,
    process_turn,
)
from app.agent.history import (
    NON_TEXT_PLACEHOLDER,
    VOICE_NOTE_PLACEHOLDER,
    HistoryEntry,
    content_for,
    to_chat_messages,
)
from app.agent.loop import MAX_MODEL_CALLS, MAX_TOOL_CALLS_PER_TURN
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.agent.tools import (
    MAX_BOOKING_CHANGES_PER_TURN,
    MIN_SECONDS_FOR_A_BOOKING_CHANGE,
    BookingOutcome,
    BookingState,
    ChangePhase,
    ChangeStatus,
    PatientContext,
    ToolCallRecord,
    ToolContext,
    ToolExecutionStatus,
    default_registry,
)

__all__ = [
    "CLINIC_TIMEZONE",
    "CLINIC_TZ",
    "MAX_BOOKING_CHANGES_PER_TURN",
    "MAX_MODEL_CALLS",
    "MAX_TOOL_CALLS_PER_TURN",
    "MIN_SECONDS_FOR_A_BOOKING_CHANGE",
    "NON_TEXT_PLACEHOLDER",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_VERSION",
    "VOICE_NOTE_PLACEHOLDER",
    "AgentResult",
    "AgentRuntime",
    "BookingOutcome",
    "BookingState",
    "ChangePhase",
    "ChangeStatus",
    "Clock",
    "HistoryEntry",
    "PatientContext",
    "ToolCallRecord",
    "ToolContext",
    "ToolExecutionStatus",
    "Turn",
    "build_messages",
    "clock_message",
    "content_for",
    "default_registry",
    "process_turn",
    "to_chat_messages",
    "utc_now",
]
