"""Jobs that exhausted their retries.

Hard rule 11: jobs that keep failing land here instead of retrying forever.
A row is a thing a human looks at, not something the worker reads back.
"""

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
    tenant_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    job_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # The webhook_inbox row this job came from: its id, as a string.
    #
    # VS-002 wrote "provider_event_id" here. VS-004 changed it to the row id
    # (plan note C2): a provider_event_id is a wamid, wamids are base64 and
    # decode to include a phone number, and this is a table people open casually
    # to triage failures. The row id is also the better join key.
    #
    # Deliberately not a foreign key: the inbox row may be pruned by a retention
    # policy long before anyone reviews the dead letter.
    source_event_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # A REFERENCE to the event, never a copy of it.
    #
    # VS-002 assumed this would hold the same patient content as
    # webhook_inbox.payload. VS-004 requirement 6 forbids that (plan note C7): a
    # triage table must not become a second copy of a patient's message, under a
    # second retention policy, read by more people. What goes here is
    # {inbox_row_id, kind, phone_number_id, job_try} - see
    # app/worker/jobs/inbox.py:dead_letter_payload. source_event_id points at the
    # row that has the full event, so nothing is lost.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
