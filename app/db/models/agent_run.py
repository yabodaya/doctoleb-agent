"""What one generated turn cost, and which tools it called.

Two tables with a single design rule, which is hard rule 8 taken literally:
**nothing here may hold content.** No patient text, no model text, no tool
arguments, no tool results, no doctor names, no slot times, no OpenAI tool-call
ids. Only codes, counts, durations and our own row UUIDs.

That rule is what makes these tables safe to open casually - to answer "what did
last week cost?" or "why did that turn fail?" - without becoming a second copy
of the conversation under a second retention policy. The exact column set of
both tables is pinned by a test, so adding a `result` or `arguments` column
means consciously editing it.

Rows are written in T1b, in the same transaction as the reply reservation, but
inside a SAVEPOINT: bookkeeping that fails must never cost the patient a reply.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import AgentRunOutcome, ToolExecutionStatus, check_constraint


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per generated turn that reached T1b.

    An attempt that ends RETRYABLE with tries left raises before T1b and is NOT
    recorded (Q1): its model calls were billed and are invisible here. That gap
    is a known follow-up, not an oversight.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        check_constraint("outcome", AgentRunOutcome, "outcome_valid"),
        sa.Index("ix_agent_runs_tenant_id_created_at", "tenant_id", "created_at"),
        sa.Index("ix_agent_runs_inbox_event_id", "inbox_event_id"),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The webhook_inbox row - the same value every log line carries as
    # `event_id=`, which is what makes a log line and a row joinable by hand.
    #
    # Deliberately NOT a foreign key, for the same reason as
    # dead_letter_jobs.source_event_id: a retention policy may prune the inbox
    # long before anyone reads these cost records.
    inbox_event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    # NULL when hard rule 7 dropped the reply: the turn ran, was billed, and
    # nothing was sent because a human had taken the conversation over.
    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    job_try: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The CONFIGURED OPENAI_CHAT_MODEL (Q10), NULL when unset. Not the snapshot
    # the response reports serving: that is a follow-up, and this is the value
    # an operator can act on.
    model: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    prompt_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # A reason CODE, never a message. The repository truncates it to the column.
    reason: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # Calls STARTED, so a call cut off by the turn deadline still counts: it was
    # billed.
    model_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Summed over every model call of the turn that reported them. NULL when the
    # API reported none.
    prompt_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Measured by the job around process_turn, so it includes the tool calls.
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class ToolExecution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tool call the model asked for, executed or not."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        check_constraint("status", ToolExecutionStatus, "status_valid"),
        # Q9. All of a turn's rows are written in ONE transaction, and
        # PostgreSQL's now() is constant within a transaction, so created_at
        # cannot order them. This unique key both fixes the order and indexes
        # the foreign key.
        sa.UniqueConstraint("agent_run_id", "sequence", name="uq_tool_executions_run_sequence"),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    # The same value as the run's. Denormalised on purpose: it makes these rows
    # tenant-scoped on their own, so a per-tenant query or deletion never has to
    # trust a join.
    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 0-based, within the turn
    model_call: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 1-based: which response
    # A REGISTERED tool name, or the literal "unknown". The name the model sent
    # is model-written text and could carry a patient's words, so an
    # unrecognised one is never stored (Q9).
    #
    # No CHECK constraint: VS-007's tools would each need a migration. The
    # registry is the authority, and tests prove nothing else is written.
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # DECLARED parameter names that were present, sorted. Never values, and
    # never an undeclared key - that key is model-written too.
    argument_names: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)
