"""One ongoing thread between a clinic and a patient on one channel."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, ConversationState, check_constraint

OPEN_STATES = (
    ConversationState.AI_ACTIVE,
    ConversationState.HUMAN_REQUESTED,
    ConversationState.HUMAN_ACTIVE,
)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        check_constraint("state", ConversationState, "state_valid"),
        check_constraint("channel", Channel, "channel_valid"),
        # Review Focus 3. At-least-once jobs plus Meta's duplicate and
        # out-of-order delivery mean two workers can both find no open
        # conversation and both insert one. Partial, so closing a conversation
        # frees the slot for the next one.
        sa.Index(
            "uq_conversations_open",
            "tenant_id",
            "contact_id",
            "channel",
            unique=True,
            postgresql_where=sa.text("state <> 'CLOSED'"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default=ConversationState.AI_ACTIVE.value
    )
    state_changed_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # WhatsApp allows free-form replies only within 24h of the patient's last
    # message (docs/architecture.md). VS-004 needs this to decide between a
    # free-form reply and a template; storing it now costs one column.
    last_inbound_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
