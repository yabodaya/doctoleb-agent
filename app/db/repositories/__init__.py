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
from app.db.repositories.contacts import ContactRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.dead_letter import DeadLetterJobRepository
from app.db.repositories.errors import (
    DuplicateRecordError,
    RepositoryError,
    RunNotRecordedError,
    as_duplicate,
)
from app.db.repositories.messages import MessageRepository
from app.db.repositories.webhook_inbox import ClaimResult, WebhookInboxRepository

__all__ = [
    "AgentRunRepository",
    "AgentRunRow",
    "ClaimResult",
    "ContactRepository",
    "ConversationRepository",
    "DeadLetterJobRepository",
    "DuplicateRecordError",
    "MessageRepository",
    "RepositoryError",
    "RunNotRecordedError",
    "ToolExecutionRow",
    "WebhookInboxRepository",
    "as_duplicate",
]
