"""Jobs that exhausted their retries.

Hard rule 11: jobs that keep failing land here instead of retrying forever.
A row is a thing a human looks at, not something the worker reads back.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DeadLetterJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dead_letter_jobs"
    __table_args__ = (sa.Index("ix_dead_letter_jobs_created_at", "created_at"),)

    # Nullable: a job can die before tenant resolution succeeds, and that is
    # precisely the failure most worth recording.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    job_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # The webhook_inbox.provider_event_id this job came from, when there is one.
    # Deliberately not a foreign key: the inbox row may be pruned by a retention
    # policy long before anyone reviews the dead letter.
    source_event_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Same patient content as webhook_inbox.payload. The retention follow-up
    # covers both tables.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
