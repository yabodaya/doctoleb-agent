"""Messages in both directions, and the history VS-005 reads."""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.db.enums import (
    STATUS_RANK,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Conversation, Message
from app.db.repositories.base import TenantScopedRepository
from app.db.repositories.errors import as_duplicate


class MessageRepository(TenantScopedRepository):
    async def add(
        self,
        conversation_id: uuid.UUID,
        direction: MessageDirection,
        modality: MessageModality,
        status: MessageStatus,
        text: str | None = None,
        provider_message_id: str | None = None,
    ) -> Message:
        """Store one message.

        A duplicate provider_message_id becomes DuplicateRecordError, which
        carries the constraint name only — the raw IntegrityError quotes row
        values, and this row's values are patient content (hard rule 8).

        Wrapped in a SAVEPOINT (plan amendment A2). In PostgreSQL a failed
        INSERT aborts the WHOLE transaction, so without one the caller that
        catches DuplicateRecordError and re-reads the existing row - which is
        exactly how VS-004's job makes itself re-runnable - would get
        InFailedSqlTransaction on that re-read instead. begin_nested() rolls back
        only the failed INSERT and leaves the surrounding transaction usable.
        """
        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            direction=direction.value,
            modality=modality.value,
            status=status.value,
            text=text,
            provider_message_id=provider_message_id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(message)
                await self._session.flush()
        except IntegrityError as error:
            raise as_duplicate(error) from None

        if direction is MessageDirection.INBOUND:
            # The 24h WhatsApp free-form window is measured from the patient's
            # last message (docs/architecture.md). Only inbound moves it.
            await self._session.execute(
                sa.update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == self.tenant_id,
                )
                .values(last_inbound_at=sa.func.now())
            )
        return message

    async def set_transcript(self, message_id: uuid.UUID, text: str) -> bool:
        """Write a voice note's transcript onto the message it belongs to (VS-008).

        The transcript IS the patient's message, so it goes in the same column a
        typed one uses, under the same retention and the same access. There is
        no second place it lives (hard rule 8).

        `AND text IS NULL` in the WHERE clause rather than a read-then-write, for
        two reasons. Two workers cannot then disagree about what the patient
        said: the first write wins and the second is a no-op, instead of the
        last one overwriting a transcript somebody has already built a turn
        from. And a retry that somehow reaches this with the row already written
        finds it written and moves on.

        Returns whether it updated. The caller does not care today - "already
        written" and "just written" are the same situation - but the return
        value is what lets a test tell them apart.

        Deliberately NOT inside a savepoint, unlike the `voice_notes` write that
        accompanies it. If this fails the transaction must fail and the job must
        retry: a turn built from a transcript nobody stored is exactly what
        writing it immediately exists to prevent, because the retry would pay
        for a second transcription and could hear something different.
        """
        result = await self._session.execute(
            sa.update(Message)
            .where(
                Message.id == message_id,
                Message.tenant_id == self.tenant_id,
                Message.text.is_(None),
            )
            .values(text=text)
        )
        return result.rowcount == 1

    async def get_by_provider_id(self, provider_message_id: str) -> Message | None:
        return await self._session.scalar(
            sa.select(Message).where(
                Message.provider_message_id == provider_message_id,
                Message.tenant_id == self.tenant_id,
            )
        )

    async def recent(self, conversation_id: uuid.UUID, limit: int) -> list[Message]:
        """The newest `limit` messages, returned oldest first.

        Newest-first in SQL so the index does the work and the LIMIT is cheap;
        reversed in Python so the caller gets chat order.
        """
        result = await self._session.scalars(
            sa.select(Message)
            .where(
                Message.tenant_id == self.tenant_id,
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(result.all()))

    async def history_before(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID, limit: int
    ) -> list[Message]:
        """The `limit` newest messages of a conversation created before one
        message, oldest first - what the model sees before the message it is
        answering (VS-005 plan assumption A8).

        Anchored on the answered message's created_at IN SQL, through its id,
        rather than on the ORM attribute: after an INSERT the server-default
        created_at may not be loaded, and loading it under AsyncSession raises
        MissingGreenlet.

        Failed outbound messages are skipped here, not in Python, so `limit`
        counts only what the model will see (requirement 5). Without that, a
        conversation with a run of failed replies would reach the model with
        almost no memory at all.

        Served by ix_messages_tenant_id_conversation_id_created_at, which
        VS-002 added for this query.
        """
        if limit <= 0:
            return []
        anchor = aliased(Message)
        anchor_created_at = (
            sa.select(anchor.created_at)
            .where(anchor.id == message_id, anchor.tenant_id == self.tenant_id)
            .scalar_subquery()
        )
        result = await self._session.scalars(
            sa.select(Message)
            .where(
                Message.tenant_id == self.tenant_id,
                Message.conversation_id == conversation_id,
                Message.created_at < anchor_created_at,
                sa.not_(
                    sa.and_(
                        Message.direction == MessageDirection.OUTBOUND.value,
                        Message.status == MessageStatus.FAILED.value,
                    )
                ),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(result.all()))

    async def reserve_reply(
        self, conversation_id: uuid.UUID, reply_to_message_id: uuid.UUID, text: str
    ) -> Message:
        """Claim the right to reply to one inbound message.

        Returns the reserved row, or the row somebody else already reserved. The
        caller then looks at its provider_message_id: set means the reply is
        already out and must not be sent again (VS-004 requirement 3).

        ON CONFLICT DO NOTHING rather than catching IntegrityError, for the same
        two reasons as ContactRepository.get_or_create_by_identity: the database
        resolves the race with no window, and no exception quoting this row's
        values - which include the reply text and the conversation ids - is ever
        raised (hard rule 8). It is also the plan's amendment A2 satisfied by
        construction: nothing fails, so nothing aborts the transaction.

        Written and COMMITTED before the Meta call, so a crash during the send
        leaves a durable record that a reply is in flight. See "Commit
        boundaries" in docs/plans/VS-004-plan.md.
        """
        statement = (
            pg_insert(Message)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND.value,
                modality=MessageModality.TEXT.value,
                status=MessageStatus.QUEUED.value,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
            .on_conflict_do_nothing(constraint="uq_messages_reply_to_message_id")
            .returning(Message)
        )
        reserved = (await self._session.execute(statement)).scalar_one_or_none()
        if reserved is not None:
            return reserved

        existing = await self.get_reply_to(reply_to_message_id)
        assert existing is not None  # the conflict proves the row exists
        return existing

    async def get_reply_to(self, reply_to_message_id: uuid.UUID) -> Message | None:
        """The reply to one inbound message, if one has been reserved."""
        return await self._session.scalar(
            sa.select(Message).where(
                Message.reply_to_message_id == reply_to_message_id,
                Message.tenant_id == self.tenant_id,
            )
        )

    async def attach_provider_id(self, message_id: uuid.UUID, provider_message_id: str) -> None:
        """Record that Meta accepted this message, and the id it gave it.

        Status and timestamp move in the same statement as the wamid: the three
        facts are one fact, and a row that carries a wamid while still saying
        QUEUED would make a retry believe the send never happened.
        """
        await self._session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.tenant_id == self.tenant_id)
            .values(
                provider_message_id=provider_message_id,
                status=MessageStatus.SENT.value,
                sent_at=sa.func.now(),
            )
        )

    async def mark_sent_without_id(self, message_id: uuid.UUID) -> None:
        """Meta accepted this message but gave us no id we could read.

        SENT, with provider_message_id left NULL. Not a retry: the message is
        already on its way, and asking again sends a second copy. The cost is
        that no status callback will ever match this row - see "The
        duplicate-reply gap" in docs/plans/VS-004-plan.md.
        """
        await self._session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.tenant_id == self.tenant_id)
            .values(status=MessageStatus.SENT.value, sent_at=sa.func.now())
        )

    async def mark_failed(self, message_id: uuid.UUID) -> None:
        """Meta refused this message permanently.

        Hard rule 5's shape: the row says FAILED and carries no wamid, so nothing
        anywhere claims the patient was told something they were not.
        """
        await self._session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.tenant_id == self.tenant_id)
            .values(status=MessageStatus.FAILED.value)
        )

    async def advance_status(self, provider_message_id: str, status: MessageStatus) -> bool:
        """Move a message forward, never backwards (VS-004 requirement 5).

        The rank comparison is in the WHERE clause, not in Python, so two status
        callbacks arriving at once cannot interleave a read and a write and lose
        one. Meta delivers `delivered` after `read` routinely, and the ranks in
        app/db/enums.py are what make that a no-op rather than a regression.

        Returns True when the row moved. False means either "already at or past
        this status", which is fine, or "no such message for this tenant" - and
        the caller has already established which, because it looks the message up
        first to decide whether the status was simply early.
        """
        current_rank = sa.case(
            {member.value: rank for member, rank in STATUS_RANK.items()},
            value=Message.status,
            else_=0,
        )
        result = await self._session.execute(
            sa.update(Message)
            .where(
                Message.provider_message_id == provider_message_id,
                Message.tenant_id == self.tenant_id,
                current_rank < STATUS_RANK[status],
            )
            .values(status=status.value)
        )
        return result.rowcount == 1
