"""The thread, and the state machine hard rule 7 re-reads before every send."""

import uuid

import sqlalchemy as sa

from app.db.enums import ConversationState
from app.db.models import Conversation
from app.db.repositories.base import TenantScopedRepository

CLOSED = ConversationState.CLOSED.value


class ConversationRepository(TenantScopedRepository):
    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._session.scalar(
            sa.select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
        )

    async def current_state(self, conversation_id: uuid.UUID) -> str | None:
        """Read this conversation's state FROM THE DATABASE, always.

        Hard rule 7 re-reads the state immediately before sending, to catch a
        human taking the thread over while the job was doing something else. `get`
        cannot serve that purpose in the session that already loaded the
        conversation: a `select()` for a mapped entity is resolved through
        SQLAlchemy's identity map, so it returns the instance that is already
        there - with the state it had when it was loaded - and never notices that
        another transaction has committed a change since.

        Selecting the COLUMN instead of the entity sidesteps the identity map
        entirely: there is no instance to return, so the value can only come from
        the round trip. `populate_existing()` on the entity query would work too;
        this is the narrower tool, and the return type says what the caller
        actually needs.

        Returns None for a missing conversation, or for another tenant's id.
        """
        return await self._session.scalar(
            sa.select(Conversation.state).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
        )

    async def get_open(self, contact_id: uuid.UUID, channel: str) -> Conversation | None:
        """The one non-CLOSED conversation, if there is one.

        "The one" is guaranteed by the partial unique index, not by this query.
        """
        return await self._session.scalar(
            sa.select(Conversation).where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.contact_id == contact_id,
                Conversation.channel == str(channel),
                Conversation.state != CLOSED,
            )
        )

    async def get_or_create_open(self, contact_id: uuid.UUID, channel: str) -> Conversation:
        """Check-then-insert.

        A concurrent writer can win between the check and the insert, in which
        case the partial unique index raises IntegrityError and it propagates
        unchanged. That is deliberate: the caller (VS-004's job) treats it as
        retryable and re-reads, rather than dead-lettering. The conflicting
        values are ids only, so the raw error is not a hard-rule-8 exposure.

        The insert runs inside a SAVEPOINT (VS-004 plan amendment A2). In
        PostgreSQL a failed INSERT aborts the WHOLE transaction, so without one
        the IntegrityError would leave the caller's session unusable - every
        subsequent statement raising InFailedSqlTransaction, including whatever
        the caller does on its way out. begin_nested() rolls back only the failed
        INSERT; the exception still propagates, and the session it propagates
        through still works.
        """
        existing = await self.get_open(contact_id, channel)
        if existing is not None:
            return existing
        conversation = Conversation(
            tenant_id=self.tenant_id,
            contact_id=contact_id,
            channel=str(channel),
            state=ConversationState.AI_ACTIVE.value,
        )
        async with self._session.begin_nested():
            self._session.add(conversation)
            await self._session.flush()
        return conversation

    async def set_state(
        self, conversation_id: uuid.UUID, state: ConversationState
    ) -> Conversation | None:
        """Move the conversation and record when. Returns None for another
        tenant's id, so a tenant-resolution bug changes nothing."""
        result = await self._session.execute(
            sa.update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
            .values(state=state.value, state_changed_at=sa.func.now())
            .returning(Conversation)
        )
        updated = result.scalar_one_or_none()
        if updated is not None:
            await self._session.refresh(updated)
        return updated
