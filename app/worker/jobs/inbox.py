"""The one job VS-004 adds: process one `webhook_inbox` row.

Read `docs/plans/VS-004-plan.md`, sections "Commit boundaries" and "Idempotency:
the four keys", before changing the order of anything in here. The ordering is
the correctness.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from arq.worker import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.whatsapp.client import MetaClient
from app.channels.whatsapp.payloads import InboxItemKind
from app.config import Settings
from app.db.enums import InboxStatus
from app.db.repositories import DeadLetterJobRepository, WebhookInboxRepository
from app.tenants import TenantId, TenantMapError, TenantResolver, UnknownPhoneNumberError
from app.worker.errors import PermanentJobError, RetryableJobError
from app.worker.retrying import backoff_seconds

logger = logging.getLogger(__name__)

JOB_NAME = "process_inbox_event"


@dataclass(frozen=True)
class EventContext:
    """Everything a handler needs, resolved once by the envelope.

    A frozen dataclass rather than six positional arguments, because the handlers
    each open more than one transaction and every one of them needs the same
    five things.
    """

    event_id: uuid.UUID
    tenant_id: TenantId
    phone_number_id: str
    payload: dict[str, Any]
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    meta: MetaClient

    @property
    def item(self) -> Any:
        """The raw Meta message or status object, exactly as it arrived."""
        return self.payload.get("item")


async def process_inbox_event(ctx: dict[str, Any], row_id: str) -> str:
    """Process one webhook_inbox row. Returns a short outcome code.

    Takes OUR row id, never a payload and never Meta's event id (hard rule 8,
    plan note C2): a wamid is base64 and decodes to include the patient's phone
    number, so it must not reach Redis, the job arguments, or the five log lines
    a retrying job writes. The row - and with it the payload, the wamid and the
    phone number - is loaded from Postgres, where it already is.

    Idempotent four ways over: arq refuses a duplicate job id, claim() refuses an
    event already PROCESSED, claim()'s lease refuses an event another worker is
    working on right now, and the reply's unique constraint refuses a second
    reply row.

    The return value is a code, not a sentence: arq stores a job result in Redis,
    so it must be as safe to keep as a log line.
    """
    job_try = max(int(ctx.get("job_try", 1)), 1)
    settings: Settings = ctx["settings"]
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    resolver: TenantResolver = ctx["resolver"]
    meta: MetaClient = ctx["meta"]
    event_id = uuid.UUID(row_id)

    kind: str | None = None
    phone_number_id: str | None = None
    holds_lease = False

    try:
        # --- T0: claim, and COMMIT IT ON ITS OWN (plan amendment A1) --------
        # Not folded into the work below. A claim inside that transaction is
        # rolled back with it on every retryable error, which undoes attempts + 1
        # (so the dead letter under-reports the retry curve) and holds a write
        # lock on the row for the whole of it (so a concurrent worker BLOCKS
        # instead of being told `locked` at once). A lease only does its job if
        # other transactions can see it, and in PostgreSQL that means committed.
        async with sessionmaker() as session:
            claim = await WebhookInboxRepository(session).claim(
                event_id, settings.claim_lease_seconds
            )
            await session.commit()
            row = claim.row

        if claim.state == "already_processed":
            logger.info("inbox event already processed event_id=%s", event_id)
            return "skipped"
        if claim.state == "locked":
            # Another worker holds a live lease. Defer rather than duplicate its
            # work - and never release, because the lease is not ours. If that
            # worker dies, the lease expires and this event becomes claimable.
            logger.info("inbox event locked by another worker event_id=%s", event_id)
            raise RetryableJobError("event_locked")
        if claim.state == "missing":
            # The webhook commits before it enqueues, so this should not happen.
            # Retryable rather than permanent: a retry costs four deferrals and
            # ends in a dead letter either way, while calling it permanent on the
            # first try would discard an event that a visibility oddity had
            # merely hidden.
            raise RetryableJobError("inbox_row_missing")

        holds_lease = True
        assert row is not None  # state == "claimed"
        payload = row.payload if isinstance(row.payload, dict) else {}

        # VS-003's note, obeyed: the kind is an explicit field. Never split out
        # of provider_event_id - that key format is ours and may change, and the
        # job does not even receive it.
        kind = payload.get("kind")
        phone_number_id = _phone_number_id(payload)
        tenant_id = _resolve_tenant(resolver, phone_number_id)

        handler = _handler_for(kind)
        if handler is None:
            raise PermanentJobError("unknown_kind")

        context = EventContext(
            event_id=event_id,
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            payload=payload,
            settings=settings,
            sessionmaker=sessionmaker,
            meta=meta,
        )
        outcome = await handler(context)
        logger.info("inbox event done event_id=%s kind=%s outcome=%s", event_id, kind, outcome)
        return outcome

    except RetryableJobError as error:
        # Release on the way out (plan assumption A17). Deferring while still
        # holding the lease would make the retry arrive, find its own stale
        # lease, report `locked` against itself and defer again - one backoff
        # curve turned into max_tries lease timeouts. Skipped when the lease is
        # somebody else's.
        if holds_lease:
            await _release(sessionmaker, event_id)
        if job_try < settings.job_max_tries:
            defer = backoff_seconds(job_try, settings)
            logger.warning(
                "inbox event retrying event_id=%s reason=%s try=%d defer=%.1f",
                event_id,
                error.reason,
                job_try,
                defer,
            )
            # arq does NOT retry a plain exception (plan note C13). Raising Retry
            # is how a retry happens at all, and raising it ourselves is also
            # what gives us a backoff curve we control.
            raise Retry(defer=defer) from None
        await _dead_letter(sessionmaker, event_id, kind, phone_number_id, job_try, error.reason)
        return "dead_lettered"

    except PermanentJobError as error:
        if holds_lease:
            await _release(sessionmaker, event_id)
        # No retry, and no re-raise: raising would make arq log a traceback for a
        # decision we have already recorded properly.
        await _dead_letter(sessionmaker, event_id, kind, phone_number_id, job_try, error.reason)
        return "dead_lettered"


def _handler_for(kind: str | None):
    """Dispatch on payload["kind"], resolved through module globals at call time.

    Looked up by name rather than from a dict built at import, so a test can
    monkeypatch a handler and this actually sees the replacement.
    """
    if kind == InboxItemKind.MESSAGE.value:
        return handle_message
    if kind == InboxItemKind.STATUS.value:
        return handle_status
    return None


def _phone_number_id(payload: dict[str, Any]) -> str:
    """metadata.phone_number_id, or a permanent failure.

    Hard rule 4 starts here: without it there is no tenant, and there is no
    default tenant to fall back to.
    """
    metadata = payload.get("metadata")
    value = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
    if not isinstance(value, str) or not value:
        raise PermanentJobError("no_phone_number_id")
    return value


def _resolve_tenant(resolver: TenantResolver, phone_number_id: str) -> TenantId:
    """Hard rule 4. Both failures are permanent, and for the same reason: there
    is no tenant to guess at, and guessing is the one unacceptable answer."""
    try:
        return resolver.resolve(phone_number_id)
    except UnknownPhoneNumberError:
        logger.error("no tenant for phone_number_id=%s", phone_number_id)
        raise PermanentJobError("unknown_phone_number") from None
    except TenantMapError as error:
        logger.error("tenant map unusable reason=%s", error.reason)
        raise PermanentJobError(error.reason) from None


async def _release(sessionmaker: async_sessionmaker[AsyncSession], event_id: uuid.UUID) -> None:
    """Give the lease back, in a FRESH session.

    The session the failure happened in has been rolled back or abandoned; this
    needs one that works.
    """
    async with sessionmaker() as session:
        await WebhookInboxRepository(session).release(event_id)
        await session.commit()


def dead_letter_payload(
    row_id: uuid.UUID, kind: str | None, phone_number_id: str | None, job_try: int
) -> dict[str, Any]:
    """A REFERENCE to the event, never the event (requirement 6, plan note C7).

    dead_letter_jobs.payload is NOT NULL, and VS-002's docstring assumed it would
    hold a copy of webhook_inbox.payload. Requirement 6 forbids that: this is a
    table people open to triage failures, and it must not be a second copy of a
    patient's message. source_event_id points at the inbox row that has the full
    event, so nothing is lost and the sensitive copy stays in one place under one
    retention policy.

    It carries our row id, NOT provider_event_id (plan note C2): a wamid decodes
    to include a phone number, and a triage table is read casually.
    """
    return {
        "inbox_row_id": str(row_id),
        "kind": kind,
        "phone_number_id": phone_number_id,
        "job_try": job_try,
    }


async def _dead_letter(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: uuid.UUID,
    kind: str | None,
    phone_number_id: str | None,
    job_try: int,
    reason: str,
) -> None:
    """Hard rule 11: record the failure a human has to look at, and stop.

    A fresh session, for the same reason as _release. Marks the inbox row FAILED
    with the reason code, which also clears the lease.
    """
    logger.error(
        "inbox event dead-lettered event_id=%s kind=%s reason=%s attempts=%d",
        event_id,
        kind,
        reason,
        job_try,
    )
    async with sessionmaker() as session:
        await DeadLetterJobRepository(session).add(
            job_name=JOB_NAME,
            payload=dead_letter_payload(event_id, kind, phone_number_id, job_try),
            error=reason,
            attempts=job_try,
            source_event_id=str(event_id),
        )
        await WebhookInboxRepository(session).mark(event_id, InboxStatus.FAILED, error=reason)
        await session.commit()


# Filled in by Tasks 7 and 8. Defined here so _handler_for can name them.
async def handle_message(context: EventContext) -> str:  # pragma: no cover - Task 7
    raise NotImplementedError


async def handle_status(context: EventContext) -> str:  # pragma: no cover - Task 8
    raise NotImplementedError
