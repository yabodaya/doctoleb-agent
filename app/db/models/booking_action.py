"""The one change a conversation has prepared or executed, as ids and codes.

**Why this table exists.** The model sees earlier messages as text only, and tool
results are never stored. A hold made while answering message 1 is therefore
invisible to the model when the patient says "yes" in message 2: the `hold_id` is
gone, and asking the model to reconstruct it from its own earlier wording is asking
it to invent an id - which is exactly what hard rule 5 forbids. So OUR code keeps
the reference. T1 loads it as plain data; the job writes it after the turn. The
Agent Core still does no database access (plan V2 and V9).

**What it may hold: ids, codes and one operational expiry.** `CLAUDE.md` says this
repo does not own appointment data, and it does not: there are no names here, no
appointment or slot times, no doctor ids or names, no tool results, no patient
reference and no argument values. `hold_expires_at` is the one timestamp, and it is
an operational deadline from the Booking Service, not an appointment time. A test
pins the exact column set, and another pins that no column is unbounded `TEXT`
except `tenant_id` - so adding a column that could hold content means consciously
editing a test (hard rule 8).

**The partial unique index is the design.** At most one `PENDING` row per
conversation means "the prepared change" is a single thing. Two quick messages can
still produce two turns, and the index plus `WHERE status = 'PENDING'` on every
update is what stops them both deciding the same change (plan risk R1).
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import BookingActionKind, BookingActionStatus, check_constraint


class BookingAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "booking_actions"
    __table_args__ = (
        check_constraint("kind", BookingActionKind, "kind_valid"),
        check_constraint("status", BookingActionStatus, "status_valid"),
        # V2. Declared exactly like uq_conversations_open, which is the pattern
        # the drift test already accepts for a partial unique index.
        sa.Index(
            "uq_booking_actions_one_pending",
            "conversation_id",
            unique=True,
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
        sa.Index(
            "ix_booking_actions_tenant_id_conversation_id_created_at",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Operational state dies with its conversation, so this one IS a foreign key
    # with ON DELETE CASCADE - unlike agent_runs, whose cost records deliberately
    # outlive message retention.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # The Booking Service's hold, for a BOOK or a RESCHEDULE. NEVER shown to the
    # model: the tools that consume a hold read it from here, so the model can
    # neither invent nor reuse one (plan conflict C15).
    hold_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # The service's hold expiry. An operational deadline, NOT an appointment time,
    # and compared against the INJECTED clock, never SQL now() (plan risk R5).
    hold_expires_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The target of a RESCHEDULE or CANCEL, or the result of a BOOK once DONE.
    appointment_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # The webhook_inbox row - the `event_id=` on every log line, which is what
    # makes a log line and this row joinable by hand. No FK, for the same reason
    # as agent_runs.inbox_event_id: retention may prune the inbox.
    created_by_inbox_event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    # The inbound message whose turn PREPARED this change. No FK, same reason.
    created_by_inbound_message_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    # The inbound message whose turn EXECUTED it. NULL while still PENDING, and
    # the gate refuses to execute a change whose own message is this one.
    decided_by_inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    # The key of the latest Booking Service call made for this row. A one-way hash
    # over a random UUID, so it reveals nothing - which is what makes it safe to
    # store and to quote to a human who has to ask the Booking Service what
    # happened after an unknown outcome (plan section 5.2).
    last_idempotency_key: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # A CODE, for FAILED and UNCERTAIN. Never a message: a contract `message` can
    # quote clinic or patient data.
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
