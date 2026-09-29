"""Every message in both directions, text and (from VS-008) voice."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    MessageDirection,
    MessageModality,
    MessageStatus,
    check_constraint,
)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        check_constraint("direction", MessageDirection, "direction_valid"),
        check_constraint("modality", MessageModality, "modality_valid"),
        check_constraint("status", MessageStatus, "status_valid"),
        # One reply per inbound message, decided by the database (VS-004
        # requirement 3). Nullable AND unique is exactly right here: PostgreSQL
        # permits many NULLs under a unique constraint, so every inbound row and
        # every non-reply outbound row is unaffected, while two jobs cannot both
        # create a reply to the same message. This is what makes a retried job
        # safe without the job checking first.
        sa.UniqueConstraint("reply_to_message_id", name="uq_messages_reply_to_message_id"),
        # VS-005 reads the last N messages of a conversation on every turn.
        sa.Index(
            "ix_messages_tenant_id_conversation_id_created_at",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    modality: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=MessageModality.TEXT.value
    )
    # Unique so a replayed Meta event cannot store the same message twice
    # (hard rule 2). Nullable because an outbound message that failed before
    # Meta accepted it never receives an id; PostgreSQL permits many NULLs
    # under a unique constraint.
    provider_message_id: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, unique=True
    )
    # Patient content (hard rule 8). Also holds the transcript for a voice note
    # from VS-008 onward.
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    sent_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The inbound message this outbound message answers. NULL on every inbound
    # row, and on any outbound message that is not a reply.
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
