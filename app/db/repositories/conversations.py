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
        unchanged. That is deliberate: the caller (VS-004's job) should treat it
        as retryable and re-read, not dead-letter. The conflicting values are
        ids only, so the raw error is not a hard-rule-8 exposure.
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
