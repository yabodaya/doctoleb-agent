"""The raw-event landing table.

Hard rule 1: the webhook endpoint only verifies, dedupes, stores here, enqueues
and returns 200. Everything downstream reads from this row, not from the request.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, InboxStatus, check_constraint


class WebhookInbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = (check_constraint("status", InboxStatus, "status_valid"),)

    provider: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=Channel.WHATSAPP.value
    )
    # Hard rule 2 lives here. Unique at the database level, because two workers
    # can run "SELECT then INSERT" at the same moment and both see nothing.
    provider_event_id: Mapped[str] = mapped_column(sa.String(255), nullable=False, unique=True)
    # Nullable: the tenant is resolved from phone_number_id by the worker
    # (hard rule 4). Requiring it here would force resolution inside the
    # webhook request, which hard rule 1 forbids.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=InboxStatus.RECEIVED.value
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # A short reason code or exception class name. Never a raw provider payload
    # excerpt: that would copy patient text out of `payload` into a column that
    # gets read casually.
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
