"""Tenant-scoped data access.

Nothing above this layer writes SQL, and nothing in this layer is reachable from
the LLM: hard rule 3 gives the model tools in app/agent/tools/ only, and those
tools call services that call these repositories.
"""

from app.db.repositories.agent_runs import (
    AgentRunRepository,
    AgentRunRow,
    ToolExecutionRow,
)
from app.db.repositories.booking_actions import (
    BookingActionRepository,
    BookingOutcomeRow,
    BookingStateRow,
)
from app.db.repositories.contacts import ContactRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.dead_letter import DeadLetterJobRepository
from app.db.repositories.errors import (
    BookingStateNotRecordedError,
    DuplicateRecordError,
    RepositoryError,
    RunNotRecordedError,
    VoiceNoteNotRecordedError,
    as_duplicate,
)
from app.db.repositories.messages import MessageRepository
from app.db.repositories.voice_notes import VoiceNoteRepository, VoiceNoteRow
from app.db.repositories.webhook_inbox import ClaimResult, WebhookInboxRepository

__all__ = [
    "AgentRunRepository",
    "AgentRunRow",
    "BookingActionRepository",
    "BookingOutcomeRow",
    "BookingStateNotRecordedError",
    "BookingStateRow",
    "ClaimResult",
    "ContactRepository",
    "ConversationRepository",
    "DeadLetterJobRepository",
    "DuplicateRecordError",
    "MessageRepository",
    "RepositoryError",
    "RunNotRecordedError",
    "ToolExecutionRow",
    "VoiceNoteNotRecordedError",
    "VoiceNoteRepository",
    "VoiceNoteRow",
    "WebhookInboxRepository",
    "as_duplicate",
]
