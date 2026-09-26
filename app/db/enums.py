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


class MessageStatus(StrEnum):
    RECEIVED = "RECEIVED"  # inbound, stored
    QUEUED = "QUEUED"  # outbound, not yet handed to Meta
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


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
