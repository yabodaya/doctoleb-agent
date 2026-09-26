"""Messages in both directions, and the history VS-005 reads."""

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import MessageDirection, MessageModality, MessageStatus
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
        self._session.add(message)
        try:
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
