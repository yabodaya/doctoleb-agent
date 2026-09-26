"""The dedupe gate (hard rule 2)."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.enums import Channel, InboxStatus
from app.db.models import WebhookInbox
from app.db.repositories.base import Repository


class WebhookInboxRepository(Repository):
    async def store_if_new(
        self,
        provider_event_id: str,
        payload: dict[str, Any],
        provider: str = Channel.WHATSAPP.value,
    ) -> WebhookInbox | None:
        """Store the event, or return None if it was already stored.

        ON CONFLICT DO NOTHING rather than "SELECT then INSERT": two Meta
        deliveries can land in two workers at the same moment, and both would
        see nothing. It is also why no IntegrityError is caught here — the
        database resolves the race, so no exception carrying payload content is
        ever raised.
        """
        statement = (
            pg_insert(WebhookInbox)
            .values(
                id=uuid.uuid4(),
                provider=provider,
                provider_event_id=provider_event_id,
                payload=payload,
                status=InboxStatus.RECEIVED.value,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["provider_event_id"])
            .returning(WebhookInbox)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def mark(self, row_id: uuid.UUID, status: InboxStatus, error: str | None = None) -> None:
        """Move an inbox row to a new status.

        `error` must be a short reason code or exception class name. Never a
        payload excerpt: that would copy patient text into a column people read
        casually (hard rule 8).
        """
        await self._session.execute(
            sa.update(WebhookInbox)
            .where(WebhookInbox.id == row_id)
            .values(status=status.value, last_error=error)
        )

    async def attach_tenant(self, row_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        """Record the tenant once the worker has resolved it from phone_number_id."""
        await self._session.execute(
            sa.update(WebhookInbox).where(WebhookInbox.id == row_id).values(tenant_id=tenant_id)
        )
