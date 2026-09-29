"""The dedupe gate (hard rule 2), and the worker's claim on a row."""

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.enums import Channel, InboxStatus
from app.db.models import WebhookInbox
from app.db.repositories.base import Repository
from app.tenants import TenantId


@dataclass(frozen=True)
class ClaimResult:
    """What claiming an inbox row found.

    Four states, not a bool, because the caller treats each one differently:
    `already_processed` is a success (someone else finished the work), `locked`
    and `missing` are retryable for different reasons, and only `claimed`
    carries a row.
    """

    state: Literal["claimed", "already_processed", "locked", "missing"]
    row: WebhookInbox | None = None


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

        Clears the claim lease in the same statement. Every caller of this is a
        worker finishing with the row - PROCESSED or FAILED - and a lease left
        behind on a finished row is pure delay for whoever touches it next.
        Release the lease with release() when the status must NOT change.
        """
        await self._session.execute(
            sa.update(WebhookInbox)
            .where(WebhookInbox.id == row_id)
            .values(status=status.value, last_error=error, locked_until=None)
        )

    async def attach_tenant(self, row_id: uuid.UUID, tenant_id: TenantId) -> None:
        """Record the tenant once the worker has resolved it from phone_number_id."""
        await self._session.execute(
            sa.update(WebhookInbox).where(WebhookInbox.id == row_id).values(tenant_id=tenant_id)
        )

    async def get(self, row_id: uuid.UUID) -> WebhookInbox | None:
        """Read one row by primary key."""
        return await self._session.scalar(sa.select(WebhookInbox).where(WebhookInbox.id == row_id))

    async def get_by_event_id(self, provider_event_id: str) -> WebhookInbox | None:
        """Read one row by its Meta event key, on the unique index.

        One caller only: the webhook's duplicate path, which needs the row id of
        an item store_if_new refused to insert (VS-004 plan note C2 - the job is
        enqueued by row id, and a redelivery must reuse the SAME id).
        """
        return await self._session.scalar(
            sa.select(WebhookInbox).where(WebhookInbox.provider_event_id == provider_event_id)
        )

    async def claim(self, row_id: uuid.UUID, lease_seconds: float) -> ClaimResult:
        """Take a time-limited lease on an event, or say why we could not.

        One conditional UPDATE, not SELECT-then-UPDATE: two workers can run the
        same job at the same instant (an at-least-once queue guarantees nothing
        else), and both would see the same row. The database arbitrates, exactly
        as store_if_new lets it arbitrate the webhook's race.

        Two guards, and both are load-bearing:

        `status <> 'PROCESSED'`, and NOT `status = 'RECEIVED'`. A previous try
        that died between the job's two commits left the row PROCESSING, and that
        row MUST be reclaimable or the patient's message is never answered.
        PROCESSED is the only status that means "do not touch".

        `locked_until IS NULL OR locked_until < now()`. This is the part status
        cannot do (plan note C3a): because PROCESSING has to stay claimable for a
        DEAD try, it cannot also mean "a LIVE try owns this" - and a second
        concurrent run that claims a live PROCESSING row finds the same reserved
        reply row with no wamid and sends the same reply. The reply row's unique
        constraint does not help there: it prevents two reply ROWS, not two sends
        from one row. Only time separates the dead try from the live one.

        now() is PostgreSQL's, evaluated server-side in this statement, so a
        worker with a skewed clock cannot grant itself a longer lease, and two
        workers need not agree on the time - only the database does.

        attempts is incremented only on a successful claim: another worker's
        attempt is not this job's attempt, and a dead letter's count must be the
        number of times WE tried.

        The caller commits this immediately, before starting its own work (plan
        amendment A1). Left inside the caller's transaction, a rollback on any
        retryable error would undo the claim - attempts + 1 included, so the dead
        letter would under-report - and a concurrent worker would block on the
        row lock for the whole of that transaction instead of being told `locked`
        at once.
        """
        lease = sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)
        statement = (
            sa.update(WebhookInbox)
            .where(
                WebhookInbox.id == row_id,
                WebhookInbox.status != InboxStatus.PROCESSED.value,
                sa.or_(
                    WebhookInbox.locked_until.is_(None),
                    WebhookInbox.locked_until < sa.func.now(),
                ),
            )
            .values(
                status=InboxStatus.PROCESSING.value,
                attempts=WebhookInbox.attempts + 1,
                locked_until=lease,
            )
            .returning(WebhookInbox)
        )
        claimed = (await self._session.execute(statement)).scalar_one_or_none()
        if claimed is not None:
            return ClaimResult("claimed", claimed)

        # Nothing updated, for one of three reasons the caller must tell apart.
        # One extra read, on the unusual path only.
        existing = await self.get(row_id)
        if existing is None:
            return ClaimResult("missing")
        if existing.status == InboxStatus.PROCESSED.value:
            return ClaimResult("already_processed")
        return ClaimResult("locked")

    async def release(self, row_id: uuid.UUID) -> None:
        """Give up the lease without changing the status.

        Deliberately does not touch `status`: this runs on the retry path, where
        the row must stay PROCESSING so the next try can reclaim it. Holding the
        lease until it expired instead would make the deferred retry find its own
        stale lease, report `locked` against itself and defer again - one backoff
        curve turned into max_tries lease timeouts.
        """
        await self._session.execute(
            sa.update(WebhookInbox).where(WebhookInbox.id == row_id).values(locked_until=None)
        )
