"""Importing this package registers every table on Base.metadata.

migrations/env.py imports it for exactly that reason: autogenerate compares the
database against Base.metadata, and a model nobody imported is a table Alembic
will cheerfully propose dropping.
"""

from app.db.models.agent_run import AgentRun, ToolExecution
from app.db.models.booking_action import BookingAction
from app.db.models.contact import Contact, ContactIdentity
from app.db.models.conversation import OPEN_STATES, Conversation
from app.db.models.dead_letter import DeadLetterJob
from app.db.models.message import Message
from app.db.models.webhook_inbox import WebhookInbox

__all__ = [
    "OPEN_STATES",
    "AgentRun",
    "BookingAction",
    "Contact",
    "ContactIdentity",
    "Conversation",
    "DeadLetterJob",
    "Message",
    "ToolExecution",
    "WebhookInbox",
]
