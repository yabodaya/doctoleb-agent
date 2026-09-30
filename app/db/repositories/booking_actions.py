"""Reading and writing the one change a conversation has prepared.

Two plain-data rows cross the boundary in each direction, so `app/db/` never
imports `app/agent/` and `app/agent/` never imports `app/db/`: the job maps between
them, exactly as it already does for `ToolExecutionRow`.

    T1          -> BookingStateRow  -> the job builds the agent's BookingState
    the turn    -> BookingOutcomeRow -> T1b or T1r calls `apply`

The interesting method is `state_for`, because it computes the **confirmation
gate** in SQL. See its docstring: that one boolean is what makes hard rule 5
structural rather than a prompt instruction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.db.enums import BookingActionKind, BookingActionStatus, MessageDirection
from app.db.models import BookingAction, Message
from app.db.repositories.base import TenantScopedRepository
from app.db.repositories.errors import BookingStateNotRecordedError

# The phases and statuses a turn can report. Plain strings rather than an import
# from `app/agent/`, for the same reason as `ToolExecutionRow`'s fields.
PROPOSED = "PROPOSED"
EXECUTED = "EXECUTED"
SUCCESS = "SUCCESS"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class BookingStateRow:
    """The conversation's latest booking action, as T1 found it.

    `confirmable` is the gate's answer, computed in SQL by `state_for`. The tools
    then check a plain flag, which means the rule cannot be half-applied by a tool
    that forgot one of its four conditions.

    `hold_id` is on this row because a tool needs it, and it is `repr=False` on the
    agent-side dataclass that carries it onward: it must never reach the model
    (plan conflict C15).
    """

    action_id: uuid.UUID
    kind: str
    status: str
    confirmable: bool
    hold_id: str | None = None
    appointment_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"BookingStateRow(action_id={self.action_id}, kind={self.kind!r}, "
            f"status={self.status!r}, confirmable={self.confirmable})"
        )


@dataclass(frozen=True)
class BookingOutcomeRow:
    """The one booking change a turn made (at most one, plan V12), as plain data.

    `phase` says whether the turn PREPARED a change (a hold, or a prepared
    cancellation) or EXECUTED one. `status` is what the Booking Service said, and
    `UNCERTAIN` is a first-class answer rather than a kind of failure: a write whose
    result is unknown may have been applied, so calling it FAILED would tell the
    patient "not booked" when it may be booked (plan V6, hard rule 5).
    """

    kind: str
    phase: str
    status: str
    error_code: str | None = None
    action_id: uuid.UUID | None = None
    hold_id: str | None = None
    hold_expires_at: datetime | None = None
    appointment_id: str | None = None
    idempotency_key: str | None = None

    def __repr__(self) -> str:
        # No key, no hold and no appointment id: this object is built on the path
        # that also writes dead letters and log lines (hard rule 8).
        return (
            f"BookingOutcomeRow(kind={self.kind!r}, phase={self.phase!r}, "
            f"status={self.status!r}, error_code={self.error_code!r})"
        )


class BookingActionRepository(TenantScopedRepository):
    """Tenant-scoped like every table that knows a tenant (hard rule 4)."""

    async def expire_pending(self, conversation_id: uuid.UUID, *, now: datetime) -> int:
        """Turn every lapsed PENDING hold of this conversation into EXPIRED.

        `now` is the **injected** clock's value, never SQL `now()`. That is plan
        risk R5: the hold's expiry came from the Booking Service's clock, so it must
        be compared with the same clock the rest of the turn uses. Message ordering
        in `state_for` uses PostgreSQL's clock instead, and the two are never
        compared with each other.

        Rows with no `hold_expires_at` (a prepared cancellation) are untouched:
        nothing about them lapses.
        """
        result = await self._session.execute(
            sa.update(BookingAction)
            .where(
                BookingAction.tenant_id == self._tenant_id,
                BookingAction.conversation_id == conversation_id,
                BookingAction.status == BookingActionStatus.PENDING.value,
                BookingAction.hold_expires_at.is_not(None),
                BookingAction.hold_expires_at <= now,
            )
            .values(status=BookingActionStatus.EXPIRED.value)
        )
        return result.rowcount

    async def supersede_pending(self, conversation_id: uuid.UUID) -> int:
        """Void every PENDING change of this conversation.

        Called on both hard-rule-7 drop paths (plan section 5.12). A staff message
        sent after the AI prepared a hold would otherwise satisfy the gate's "a
        reply was sent in between", and the patient's next "yes" would confirm
        something the AI prepared and a human never saw.
        """
        result = await self._session.execute(
            sa.update(BookingAction)
            .where(
                BookingAction.tenant_id == self._tenant_id,
                BookingAction.conversation_id == conversation_id,
                BookingAction.status == BookingActionStatus.PENDING.value,
            )
            .values(status=BookingActionStatus.SUPERSEDED.value)
        )
        return result.rowcount

    async def state_for(
        self, conversation_id: uuid.UUID, inbound_message_id: uuid.UUID
    ) -> BookingStateRow | None:
        """The latest booking action of this conversation, and whether it may be
        executed while answering `inbound_message_id`.

        **The confirmation gate (plan V3).** A change prepared while answering
        message *M* may be executed while answering message *N* only if:

        1. the row is still `PENDING`;
        2. *N* is not *M*;
        3. some reply of ours in this conversation was **actually sent** after the
           row was written and before *N* was stored;
        4. for holds, the expiry has not passed - which `expire_pending` has
           already turned into `EXPIRED`, so condition 1 covers it.

        Conditions 1-3 are the `confirmable` boolean below. Condition 3 is the one
        that does the real work, and it is stronger than "an earlier message": it
        also blocks two quick messages ("book 14:00" then "Rami", the second typed
        before our question arrived), and a hold whose describing reply was dropped
        by a takeover or refused by Meta. A patient cannot confirm something they
        were never told.

        All three timestamps come from PostgreSQL's clock, so they are comparable:
        the row's `created_at` is T1b's `now()`, the reply's `sent_at` is set in T2
        after Meta accepted it (`attach_provider_id` and `mark_sent_without_id` are
        the only two methods that set it, and `mark_failed` does not), and *N*'s
        `created_at` is its own job's T1.

        Returns plain data, not an entity: with `expire_on_commit=False` an entity
        loaded here would never see another transaction's commit (plan risk R2).
        """
        answered = (
            sa.select(Message.created_at)
            .where(Message.id == inbound_message_id, Message.tenant_id == self._tenant_id)
            .scalar_subquery()
        )
        reply_sent_in_between = (
            sa.select(sa.literal(1))
            .select_from(Message)
            .where(
                Message.tenant_id == self._tenant_id,
                Message.conversation_id == BookingAction.conversation_id,
                Message.direction == MessageDirection.OUTBOUND.value,
                # A reply that failed never carries sent_at, so it never counts.
                Message.sent_at.is_not(None),
                Message.sent_at >= BookingAction.created_at,
                Message.sent_at < answered,
            )
            .exists()
        )
        confirmable = sa.and_(
            BookingAction.status == BookingActionStatus.PENDING.value,
            BookingAction.created_by_inbound_message_id != inbound_message_id,
            reply_sent_in_between,
        )
        row = (
            await self._session.execute(
                sa.select(
                    BookingAction.id,
                    BookingAction.kind,
                    BookingAction.status,
                    BookingAction.hold_id,
                    BookingAction.appointment_id,
                    confirmable.label("confirmable"),
                )
                .where(
                    BookingAction.tenant_id == self._tenant_id,
                    BookingAction.conversation_id == conversation_id,
                )
                .order_by(BookingAction.created_at.desc(), BookingAction.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        return BookingStateRow(
            action_id=row.id,
            kind=row.kind,
            status=row.status,
            confirmable=bool(row.confirmable),
            hold_id=row.hold_id,
            appointment_id=row.appointment_id,
        )

    async def apply(
        self,
        outcome: BookingOutcomeRow,
        *,
        conversation_id: uuid.UUID,
        inbox_event_id: uuid.UUID,
        inbound_message_id: uuid.UUID,
        confirmable: bool,
    ) -> uuid.UUID | None:
        """Record one turn's booking outcome, and return the row it wrote, if any.

        Everything happens inside `session.begin_nested()` - a SAVEPOINT - for the
        reason that catches everyone: PostgreSQL aborts the WHOLE transaction on any
        failed statement. This runs in T1b **after** the reply has been reserved, so
        without the savepoint a failed bookkeeping write would poison the
        transaction and cost the patient their reply. Any `SQLAlchemyError` becomes
        `BookingStateNotRecordedError(<class name>)`, raised `from None`.

        `confirmable=False` is passed when the patient will **not** see this turn's
        own wording - the reply was dropped by hard rule 7, replaced by the guard,
        or is the fallback. A change prepared in such a turn is inserted already
        `SUPERSEDED`, because nothing described it to the patient, so nothing may
        confirm it later.

        The transitions, from plan section 5.4:

        PROPOSED, SUCCESS
            If the current PENDING row already has this `hold_id`: nothing - the
            service replayed the hold this conversation already holds (V13).
            Otherwise supersede the PENDING row and insert a new one: PENDING when
            `confirmable`, else SUPERSEDED.
        PROPOSED, UNCERTAIN
            Supersede the PENDING row; insert UNCERTAIN with `hold_id` NULL, the
            key and the error code.
        PROPOSED, FAILED
            Nothing. `tool_executions` records it, and any older PENDING row is
            still valid, because the service changed nothing.
        EXECUTED, any status
            UPDATE ... WHERE id = :action_id AND status = 'PENDING'.

        `EXECUTED` updating zero rows is **not** an error: a concurrent job already
        decided it, and `WHERE status = 'PENDING'` is exactly what makes the second
        decision a no-op (plan risk R1).
        """
        try:
            async with self._session.begin_nested():
                if outcome.phase == EXECUTED:
                    return await self._decide(outcome, inbound_message_id)
                return await self._prepare(
                    outcome,
                    conversation_id=conversation_id,
                    inbox_event_id=inbox_event_id,
                    inbound_message_id=inbound_message_id,
                    confirmable=confirmable,
                )
        except SQLAlchemyError as error:
            raise BookingStateNotRecordedError(type(error).__name__) from None

    async def _prepare(
        self,
        outcome: BookingOutcomeRow,
        *,
        conversation_id: uuid.UUID,
        inbox_event_id: uuid.UUID,
        inbound_message_id: uuid.UUID,
        confirmable: bool,
    ) -> uuid.UUID | None:
        if outcome.status == FAILED:
            # The service changed nothing, so there is nothing to record here and
            # an older PENDING row is still good.
            return None

        if outcome.status == SUCCESS and outcome.hold_id is not None:
            already = await self._session.scalar(
                sa.select(BookingAction.id).where(
                    BookingAction.tenant_id == self._tenant_id,
                    BookingAction.conversation_id == conversation_id,
                    BookingAction.status == BookingActionStatus.PENDING.value,
                    BookingAction.hold_id == outcome.hold_id,
                )
            )
            if already is not None:
                # A replayed hold: the Booking Service returned the hold this
                # conversation already has (plan V13). Inserting a second row would
                # trip the partial unique index for no gain.
                return already

        await self.supersede_pending(conversation_id)

        if outcome.status == UNCERTAIN:
            status = BookingActionStatus.UNCERTAIN.value
            # hold_id is deliberately NULL: we do not know whether a hold exists,
            # and a row that named one would let the gate offer it for confirmation.
            hold_id = None
            hold_expires_at = None
        else:
            status = (
                BookingActionStatus.PENDING.value
                if confirmable
                else BookingActionStatus.SUPERSEDED.value
            )
            hold_id = outcome.hold_id
            hold_expires_at = outcome.hold_expires_at

        action = BookingAction(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            kind=outcome.kind,
            status=status,
            hold_id=hold_id,
            hold_expires_at=hold_expires_at,
            appointment_id=outcome.appointment_id,
            created_by_inbox_event_id=inbox_event_id,
            created_by_inbound_message_id=inbound_message_id,
            last_idempotency_key=(
                outcome.idempotency_key[:64] if outcome.idempotency_key else None
            ),
            error_code=outcome.error_code[:64] if outcome.error_code else None,
        )
        self._session.add(action)
        await self._session.flush()
        return action.id

    async def _decide(
        self, outcome: BookingOutcomeRow, inbound_message_id: uuid.UUID
    ) -> uuid.UUID | None:
        if outcome.action_id is None:
            raise ValueError("an executed outcome must name the action it acted on")
        status = {
            SUCCESS: BookingActionStatus.DONE.value,
            FAILED: BookingActionStatus.FAILED.value,
            UNCERTAIN: BookingActionStatus.UNCERTAIN.value,
        }[outcome.status]
        values: dict[str, object] = {
            "status": status,
            "decided_by_inbound_message_id": inbound_message_id,
            "error_code": outcome.error_code[:64] if outcome.error_code else None,
        }
        if outcome.idempotency_key:
            values["last_idempotency_key"] = outcome.idempotency_key[:64]
        if outcome.appointment_id:
            values["appointment_id"] = outcome.appointment_id
        result = await self._session.execute(
            sa.update(BookingAction)
            .where(
                BookingAction.id == outcome.action_id,
                BookingAction.tenant_id == self._tenant_id,
                # A concurrent job that already decided this row leaves it not
                # PENDING, and this update then touches nothing. That is the
                # intended outcome, not a failure.
                BookingAction.status == BookingActionStatus.PENDING.value,
            )
            .values(**values)
        )
        return outcome.action_id if result.rowcount == 1 else None


__all__ = [
    "EXECUTED",
    "FAILED",
    "PROPOSED",
    "SUCCESS",
    "UNCERTAIN",
    "BookingActionKind",
    "BookingActionRepository",
    "BookingOutcomeRow",
    "BookingStateRow",
]
