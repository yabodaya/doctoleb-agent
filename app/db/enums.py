"""Vocabularies stored in the database.

These are VARCHAR + CHECK, not native PostgreSQL ENUM types. MessageModality
gains VOICE_NOTE in VS-008 and MessageStatus grows as VS-004 handles Meta status
callbacks; ALTER TYPE ... ADD VALUE has no reverse operation, which would make
`alembic downgrade` a lie. Swapping a CHECK constraint is fully reversible.

The .value strings are the literal contents of database rows. Renaming one is a
data migration, not a refactor.
"""

from enum import StrEnum

import sqlalchemy as sa


class Channel(StrEnum):
    WHATSAPP = "whatsapp"


class ConversationState(StrEnum):
    """docs/architecture.md: AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED."""

    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUESTED = "HUMAN_REQUESTED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    CLOSED = "CLOSED"


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageModality(StrEnum):
    TEXT = "TEXT"
    VOICE_NOTE = "VOICE_NOTE"
    # Anything else Meta can send: image, document, location, sticker, contact,
    # interactive reply. VS-004 stores every inbound type, and inventing a value
    # per type would be a CHECK migration per Meta feature. The raw type stays
    # readable in webhook_inbox.payload.
    OTHER = "OTHER"


class MessageStatus(StrEnum):
    RECEIVED = "RECEIVED"  # inbound, stored
    QUEUED = "QUEUED"  # outbound, not yet handed to Meta
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


# How far along a message is. A status callback only ever moves a message to a
# HIGHER rank (VS-004 requirement 5), enforced in the UPDATE's WHERE clause so
# there is no read-then-write race between two callbacks.
#
# FAILED sits above SENT and below DELIVERED on purpose: a send Meta later
# reports as failed must overwrite SENT, while a message that was delivered did
# not fail. RECEIVED and QUEUED share rank 0 - neither is ever advanced by a
# status callback. RECEIVED is inbound-only, and QUEUED becomes SENT by the send
# itself, not by a callback.
STATUS_RANK: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
}


class InboxStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


def check_constraint(column: str, enum_cls: type[StrEnum], name: str) -> sa.CheckConstraint:
    """Build a named CHECK restricting `column` to `enum_cls`'s values.

    Named explicitly: an anonymous CHECK gets a random PostgreSQL name, which
    makes autogenerate noisy and makes it impossible to report a violated
    constraint by name instead of by conflicting value.

    Alembic's autogenerate never compares CHECK constraints, so changing an enum
    here does NOT produce a migration on its own. Widening one is a hand-written
    migration plus a new value in the runtime test in tests/db/test_constraints.py.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


class AgentRunOutcome(StrEnum):
    """How one generated turn ended (VS-006).

    Deliberately the same three values as `ChatOutcome` in
    `app/integrations/openai/interface.py`, and a test keeps them equal. They
    are a separate enum rather than an import because `app/db/` must not depend
    on the integrations layer, and because these strings are row contents: if
    ChatOutcome ever grows a value, that is a CHECK migration, and the test is
    what makes it a decision instead of a surprise.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


class ToolExecutionStatus(StrEnum):
    """What happened to one tool call the model asked for (VS-006).

    Every call the model made gets a row, executed or not, which is why SKIPPED
    exists: calls arriving on the last allowed model response, or past the
    per-turn cap, are recorded and not run.
    """

    OK = "OK"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"  # Pydantic refused them, or they were not JSON
    UNKNOWN_TOOL = "UNKNOWN_TOOL"  # the model named a tool that is not registered
    ERROR = "ERROR"  # the tool ran and failed (booking error, crash, deadline)
    SKIPPED = "SKIPPED"  # recorded but never executed
