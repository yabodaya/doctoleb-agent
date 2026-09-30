"""The one job VS-004 adds: process one `webhook_inbox` row.

Read `docs/plans/VS-004-plan.md`, sections "Commit boundaries" and "Idempotency:
the four keys", before changing the order of anything in here. The ordering is
the correctness.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from arq.worker import Retry
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import (
    AgentResult,
    AgentRuntime,
    BookingOutcome,
    BookingState,
    Clock,
    HistoryEntry,
    ToolExecutionStatus,
    Turn,
    compose_reply,
    process_turn,
    utc_now,
)
from app.agent.tools import ChangePhase, ChangeStatus
from app.channels.whatsapp.client import MetaClient, SendOutcome
from app.channels.whatsapp.payloads import InboundMessage, InboxItemKind, StatusUpdate
from app.channels.whatsapp.redact import scrub
from app.config import Settings
from app.db.enums import (
    BookingActionKind,
    BookingActionStatus,
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Message
from app.db.repositories import (
    AgentRunRepository,
    AgentRunRow,
    BookingActionRepository,
    BookingOutcomeRow,
    ContactRepository,
    ConversationRepository,
    DeadLetterJobRepository,
    MessageRepository,
    ToolExecutionRow,
    WebhookInboxRepository,
)
from app.db.repositories.errors import (
    BookingStateNotRecordedError,
    DuplicateRecordError,
    RunNotRecordedError,
)
from app.integrations.booking import BookingClient, PatientBookingClient
from app.integrations.openai import ChatClient, ChatOutcome
from app.tenants.ids import TenantId
from app.tenants.resolver import (
    TenantMapError,
    TenantResolver,
    UnknownPhoneNumberError,
)
from app.worker.errors import PermanentJobError, RetryableJobError
from app.worker.retrying import backoff_seconds

logger = logging.getLogger(__name__)

JOB_NAME = "process_inbox_event"

# Hard rule 7: the states an AI reply is allowed in. HUMAN_REQUESTED still counts
# - the patient has asked for a human but nobody has picked the thread up yet,
# and going silent at that exact moment is the worst of both worlds.
_AI_STATES = (ConversationState.AI_ACTIVE.value, ConversationState.HUMAN_REQUESTED.value)


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
    # VS-005. `chat` is the model behind its interface - never the SDK (hard
    # rule 3). `job_try` is carried so the handler and the envelope cannot
    # disagree about which try is the last one (plan assumption A7): the
    # handler decides "fall back instead of retrying" with exactly the
    # comparison the envelope uses to decide "dead-letter instead of deferring".
    chat: ChatClient
    # VS-006. `booking` is the Booking Service behind its interface, and
    # `clock` is the one wall-clock read a turn makes (decision D3). Both are
    # injected rather than reached for, so a test can freeze the clock and hand
    # the tools a spy.
    booking: BookingClient
    clock: Clock
    # VS-007. The patient side of the Booking Service: the four writes plus the
    # patient's own list. The worker passes the SAME object as `booking`. None means
    # a worker with no booking wiring, and a booking tool then crashes the turn
    # rather than acting for a guessed identity (hard rule 4).
    patient_bookings: PatientBookingClient | None = None
    job_try: int = 1

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
    chat: ChatClient = ctx["chat"]
    booking: BookingClient = ctx["booking"]
    clock: Clock = ctx.get("clock", utc_now)
    # VS-007. `.get`, not `[...]`: a worker built before this slice, or a test that
    # cares about nothing but the read path, has no patient side - and a booking tool
    # then crashes the turn rather than acting for a guessed identity (hard rule 4).
    patient_bookings: PatientBookingClient | None = ctx.get("patient_bookings")
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
            chat=chat,
            booking=booking,
            clock=clock,
            patient_bookings=patient_bookings,
            job_try=job_try,
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
    row_id: uuid.UUID,
    kind: str | None,
    phone_number_id: str | None,
    job_try: int,
    booking: BookingOutcome | None = None,
    action_id: uuid.UUID | None = None,
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
    payload: dict[str, Any] = {
        "inbox_row_id": str(row_id),
        "kind": kind,
        "phone_number_id": phone_number_id,
        "job_try": job_try,
    }
    if booking is not None:
        # VS-007. Ids, codes and a one-way hash - nothing else. The KEY is here
        # deliberately and is the point of the whole entry: it is how a human asks
        # the Booking Service what happened to a request whose answer we never got
        # (V6). It reveals nothing, being SHA-256 over a random row UUID.
        #
        # Never here: the patient reference, the patient's name, a doctor's name, an
        # appointment time, a hold id or a reference code. This table is read
        # casually during triage (hard rule 8, plan section 5.13).
        payload["booking"] = {
            "action_id": str(action_id) if action_id else None,
            "kind": booking.kind.value,
            "phase": booking.phase.value,
            "status": booking.status.value,
            "error_code": booking.error_code,
            "idempotency_key": booking.idempotency_key,
        }
    return payload


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


def _modality_for(message_type: str | None) -> MessageModality:
    """Meta's `type` -> our modality.

    audio is mapped HERE rather than in VS-008, so a voice note stored today is
    already the right shape and VS-008 has a transcript to attach rather than a
    backfill to write.

    Everything else is OTHER. Inventing a modality per Meta feature would be a
    CHECK migration per Meta feature, and the exact type stays readable in
    webhook_inbox.payload either way.
    """
    if message_type == "text":
        return MessageModality.TEXT
    if message_type == "audio":
        return MessageModality.VOICE_NOTE
    return MessageModality.OTHER


def _display_name_for(payload: dict[str, Any], wa_id: str) -> str | None:
    """The WhatsApp profile name, matched to this message by wa_id.

    It lives in the change's `contacts` array, not on the message (VS-003's
    stored payload shape), and one change can carry several contacts.
    """
    contacts = payload.get("contacts")
    if not isinstance(contacts, list):
        return None
    for entry in contacts:
        if isinstance(entry, dict) and entry.get("wa_id") == wa_id:
            profile = entry.get("profile")
            name = profile.get("name") if isinstance(profile, dict) else None
            return name if isinstance(name, str) else None
    return None


def _validated_message(item: Any) -> InboundMessage:
    """The item as a message, or a permanent failure.

    A hash-keyed inbox row (msg:sha256:...) lands here: VS-003 stores items it
    could not read rather than dropping them, and this slice cannot process one,
    because with no wamid there is nothing to make the inbound message idempotent
    on. Dead-lettering it is what VS-003's assumption A1 predicted.
    """
    if not isinstance(item, dict):
        raise PermanentJobError("unmodelled_message")
    try:
        return InboundMessage.model_validate(item)
    except ValidationError:
        raise PermanentJobError("unmodelled_message") from None


def _history_entry(row: Message) -> HistoryEntry:
    """One stored message, reduced to what the model needs.

    Built inside T1 while the session is open, so no ORM object - and no lazy
    load - ever crosses into the agent (hard rule 3, plan conflict C2).
    """
    return HistoryEntry(
        direction=MessageDirection(row.direction),
        modality=MessageModality(row.modality),
        text=row.text,
    )


async def _record_generation_failure(
    session: AsyncSession,
    context: EventContext,
    reason: str,
    booking: BookingOutcome | None = None,
) -> None:
    """Requirement 4: the fallback is sent, and the failure is still recorded.

    Written in the SAME transaction as the fallback's reservation (or the drop),
    so both facts commit together or not at all: a crash after that commit
    re-sends the stored fallback and finds this row already written; a crash
    before it regenerates, and nothing was recorded. See "Commit boundaries" in
    docs/plans/VS-005-plan.md.

    VS-004's reference envelope (its plan note C7): codes and ids, never text.
    The inbox row is NOT marked FAILED - the patient WAS answered, with the
    fallback (VS-005 plan conflict C7). FAILED would also make the event
    claimable again, which is the wrong signal for an event that was answered.
    """
    logger.error(
        "reply generation failed event_id=%s reason=%s attempts=%d",
        context.event_id,
        reason,
        context.job_try,
    )
    await DeadLetterJobRepository(session).add(
        job_name=JOB_NAME,
        payload=dead_letter_payload(
            context.event_id,
            InboxItemKind.MESSAGE.value,
            context.phone_number_id,
            context.job_try,
            booking=booking,
        ),
        error=reason,
        attempts=context.job_try,
        tenant_id=context.tenant_id,
        source_event_id=str(context.event_id),
    )


@dataclass(frozen=True)
class GeneratedRun:
    """One generation, ready to be recorded. Set only when generation ran on
    THIS try - a retry that finds a reserved reply calls no model and records
    nothing."""

    result: AgentResult
    duration_ms: int
    job_try: int


def _tool_rows(result: AgentResult) -> tuple[ToolExecutionRow, ...]:
    """`ToolCallRecord` -> `ToolExecutionRow`.

    Two small dataclasses and a mapping here, rather than one shared type, is
    what lets `app/db/` stay ignorant of `app/agent/` and vice versa. The job is
    the only place that knows both.
    """
    return tuple(
        ToolExecutionRow(
            sequence=record.sequence,
            model_call=record.model_call,
            tool_name=record.tool_name,
            argument_names=record.argument_names,
            status=record.status.value,
            error_code=record.error_code,
            duration_ms=record.duration_ms,
        )
        for record in result.tool_calls
    )


def _final_change(result: AgentResult) -> bool:
    """Did this turn EXECUTE a change whose outcome is final enough not to redo?

    V11. Once a book, reschedule or cancel came back SUCCESS or UNCERTAIN, that
    inbound message is never generated again, even if the turn then fails
    RETRYABLE with tries left. Re-running it would bill a second time and let a
    fresh model run decide something else - hold another slot after a booking, say -
    and the patient's answer would then depend on a second model run instead of on
    the fact we already have.

    A PREPARED change (a hold, a prepared cancellation) is NOT final: a retry
    re-runs the turn, and the same key or V13 makes the repeated hold the same hold.
    """
    outcome = result.booking_outcome
    return (
        outcome is not None
        and outcome.phase is ChangePhase.EXECUTED
        and outcome.status in (ChangeStatus.SUCCESS, ChangeStatus.UNCERTAIN)
    )


def _booking_reasons(result: AgentResult) -> tuple[str, ...]:
    """Which booking dead letters this outcome owes a human.

    `booking_uncertain` for an EXECUTED change whose answer is unknown: somebody has
    to look the request up by its key and tell the patient. An uncertain HOLD is
    recorded and not dead-lettered - it expires by itself, and a retry repeats it
    under the same key, so there is nothing for a human to do.

    `booking_idempotency_conflict` is a second, separate entry for the same outcome,
    because it means something else as well: our key derivation produced the same key
    for a different body, which is a bug in this repo.
    """
    outcome = result.booking_outcome
    if outcome is None or outcome.status is not ChangeStatus.UNCERTAIN:
        return ()
    if outcome.phase is not ChangePhase.EXECUTED:
        return ()
    reasons = ["booking_uncertain"]
    if outcome.error_code == "booking_idempotency_conflict":
        reasons.append("booking_idempotency_conflict")
    return tuple(reasons)


def _outcome_row(outcome: BookingOutcome) -> BookingOutcomeRow:
    """`BookingOutcome` -> `BookingOutcomeRow`, the job's own mapping.

    Two small dataclasses instead of one shared import, exactly as for
    `ToolCallRecord`: `app/db/` must not import `app/agent/` and `app/agent/` must
    not import `app/db/`, so the job is the one place that knows both.

    The `receipt` is deliberately NOT carried across: it is content, it belongs in
    `messages.text`, and `booking_actions` holds ids and codes only.
    """
    return BookingOutcomeRow(
        kind=outcome.kind.value,
        phase=outcome.phase.value,
        status=outcome.status.value,
        error_code=outcome.error_code,
        action_id=outcome.action_id,
        hold_id=outcome.hold_id,
        hold_expires_at=outcome.hold_expires_at,
        appointment_id=outcome.appointment_id,
        idempotency_key=outcome.idempotency_key,
    )


async def _apply_booking_outcome(
    session: AsyncSession,
    context: EventContext,
    run: "GeneratedRun | None",
    *,
    conversation_id: uuid.UUID,
    inbound_id: uuid.UUID,
    confirmable: bool,
) -> uuid.UUID | None:
    """Record the turn's booking outcome, and log one line about it.

    Inside the repository's SAVEPOINT. A failure becomes
    `BookingStateNotRecordedError`, which is logged with the exception CLASS name and
    dead-lettered as `booking_state_not_recorded` - and then the reply still goes out.
    That is the right order of costs: the Booking Service has already changed
    something, the patient's receipt is built from the service's own answer rather
    than from this row, and refusing to reply would help nobody. It NEVER calls
    `session.rollback()`, which would undo the reservation (plan risk R4).

    `confirmable=False` says the patient will not see this turn's own wording - the
    reply was dropped, replaced by the guard, or is the fallback - so a change
    PREPARED in such a turn is stored already SUPERSEDED: nothing described it, so
    nothing may confirm it later.
    """
    if run is None or run.result.booking_outcome is None:
        return None
    outcome = run.result.booking_outcome
    actions = BookingActionRepository(session, context.tenant_id)
    try:
        action_id = await actions.apply(
            _outcome_row(outcome),
            conversation_id=conversation_id,
            inbox_event_id=context.event_id,
            inbound_message_id=inbound_id,
            confirmable=confirmable,
        )
    except BookingStateNotRecordedError as error:
        logger.error(
            "booking state not recorded event_id=%s error=%s",
            context.event_id,
            error.error_class,
        )
        await _add_dead_letter(
            session, context, "booking_state_not_recorded", booking=outcome, action_id=None
        )
        return None
    # Codes and OUR OWN ids only. Never a service id, a key, a name or a time.
    logger.info(
        "booking outcome event_id=%s action_id=%s kind=%s phase=%s status=%s error=%s",
        context.event_id,
        action_id,
        outcome.kind.value,
        outcome.phase.value,
        outcome.status.value,
        outcome.error_code,
    )
    for reason in _booking_reasons(run.result):
        await _add_dead_letter(session, context, reason, booking=outcome, action_id=action_id)
    return action_id


async def _add_dead_letter(
    session: AsyncSession,
    context: EventContext,
    reason: str,
    *,
    booking: BookingOutcome | None = None,
    action_id: uuid.UUID | None = None,
) -> None:
    """One dead letter, in the CALLER's transaction.

    In this transaction deliberately, not a fresh one: it has to commit or roll back
    with the reply it is about, or a crash between the two would leave a human chasing
    a change that never happened (or none for one that did).
    """
    await DeadLetterJobRepository(session).add(
        job_name=JOB_NAME,
        payload=dead_letter_payload(
            context.event_id,
            _kind_of(context),
            context.phone_number_id,
            context.job_try,
            booking=booking,
            action_id=action_id,
        ),
        error=reason,
        attempts=context.job_try,
        source_event_id=str(context.event_id),
    )


def _kind_of(context: EventContext) -> str | None:
    kind = context.payload.get("kind")
    return str(kind) if kind is not None else None


async def _record_run(
    session: AsyncSession,
    context: EventContext,
    run: GeneratedRun | None,
    *,
    inbound_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reply_message_id: uuid.UUID | None,
) -> None:
    """Write `agent_runs` and its `tool_executions`, in T1b, in a SAVEPOINT.

    Called AFTER the reservation, deliberately. The repository wraps the inserts
    in `begin_nested()`, so a bookkeeping failure rolls back only these rows and
    the patient's reply still goes out - which is the whole reason the savepoint
    is there (plan risk R4).

    It never calls `session.rollback()`: that would undo the reservation and the
    generation dead letter too.

    `run is None` means generation did not happen on this try (a retry that
    found a reserved reply), and there is nothing to record.
    """
    if run is None:
        return
    try:
        await AgentRunRepository(session, context.tenant_id).add(
            AgentRunRow(
                inbox_event_id=context.event_id,
                conversation_id=conversation_id,
                inbound_message_id=inbound_id,
                reply_message_id=reply_message_id,
                job_try=run.job_try,
                # The CONFIGURED model (Q10), NULL when unset. Blank means the
                # turn never reached OpenAI at all.
                model=context.settings.openai_chat_model.strip() or None,
                prompt_version=run.result.prompt_version,
                outcome=run.result.outcome.value,
                reason=run.result.reason,
                model_calls=run.result.model_calls,
                prompt_tokens=run.result.prompt_tokens,
                completion_tokens=run.result.completion_tokens,
                duration_ms=run.duration_ms,
                tool_executions=_tool_rows(run.result),
            )
        )
    except RunNotRecordedError as error:
        # The class name only (hard rule 8). Bookkeeping that fails must never
        # cost a patient their reply, so this is logged and swallowed.
        logger.error(
            "agent run not recorded event_id=%s error=%s", context.event_id, error.error_class
        )


async def _drop(
    session: AsyncSession,
    context: EventContext,
    inbound_id: uuid.UUID,
    conversation_id: uuid.UUID,
    failure: str | None = None,
    run: "GeneratedRun | None" = None,
    *,
    second_read: bool = False,
) -> str:
    """Hard rule 7's exit, shared by both reads.

    A reply row an earlier try reserved but never sent is marked FAILED rather
    than left QUEUED (plan conflict C9): nobody will ever send it now, and a
    QUEUED row would reach later prompts as something the clinic said. FAILED
    is also hard rule 5's shape - nothing claims the patient was told it.

    `failure` is a generation that had already failed when the takeover was
    found. It is still recorded: it still happened, and somebody still has to
    fix it. The patient simply gets a human instead of the fallback.

    The inbox row is PROCESSED: the event was handled, correctly, by not
    replying to it.
    """
    messages = MessageRepository(session, context.tenant_id)
    reserved = await messages.get_reply_to(inbound_id)
    if reserved is not None and not reserved.provider_message_id:
        await messages.mark_failed(reserved.id)

    # VS-007, plan section 5.12. On the SECOND read the turn may already have
    # changed something at the Booking Service: the loop has no database access, so
    # it could not know about the takeover. The change is the patient's own confirmed
    # request and is NOT undone - it is recorded, and a dead letter tells staff,
    # because the dead-letter table is the only staff channel until VS-010.
    if second_read and run is not None and run.result.booking_outcome is not None:
        outcome = run.result.booking_outcome
        action_id = await _apply_booking_outcome(
            session,
            context,
            run,
            conversation_id=conversation_id,
            inbound_id=inbound_id,
            # The patient will never see this turn's wording, so a change prepared
            # here can never be confirmed later.
            confirmable=False,
        )
        if outcome.phase is ChangePhase.EXECUTED and outcome.status is ChangeStatus.SUCCESS:
            await _add_dead_letter(
                session,
                context,
                "booking_changed_reply_dropped",
                booking=outcome,
                action_id=action_id,
            )

    # Both reads void every PENDING change of the conversation. A staff message sent
    # in between would otherwise satisfy the gate's "a reply was sent", and the
    # patient's next "yes" would confirm something the AI prepared and a human never
    # saw (plan section 5.12).
    await BookingActionRepository(session, context.tenant_id).supersede_pending(conversation_id)

    if failure is not None:
        await _record_generation_failure(
            session,
            context,
            failure,
            booking=run.result.booking_outcome if run is not None else None,
        )
    # Recorded with a NULL reply_message_id: the turn ran and was BILLED, and
    # nothing was sent because a human had taken over. That is a fact worth
    # keeping, not an absence.
    await _record_run(
        session,
        context,
        run,
        inbound_id=inbound_id,
        conversation_id=conversation_id,
        reply_message_id=None,
    )
    await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
    await session.commit()
    # Ids only, never the message (hard rule 7's own wording).
    logger.info(
        "reply dropped, conversation not AI-active event_id=%s conversation_id=%s",
        context.event_id,
        conversation_id,
    )
    return "dropped_not_ai_active"


async def _record_attempt(
    context: EventContext,
    run: "GeneratedRun | None",
    *,
    conversation_id: uuid.UUID,
    inbound_id: uuid.UUID,
) -> None:
    """T1r: record an attempt that made a booking change and is about to be retried.

    V9, narrowed. Q1 said a RETRYABLE attempt with tries left records nothing, and
    that still holds for READ-ONLY attempts - `test_a_turn_deadline_retries_and_
    records_nothing_until_t1b` pins it. But an attempt that made a CHANGE is
    different in kind: the Booking Service may now hold a hold, or have booked
    something, that nothing of ours knows about. Losing that with the attempt would
    leave the next try's gate looking at nothing.

    A short, DEDICATED transaction with no network call inside it, on one path only.
    The conversation row lock is taken FIRST, for the same reason as in T1b: two quick
    messages can each carry an outcome, and a lock taken after the insert could
    deadlock two writers (plan check U8).

    `confirmable=False`: the patient sees no reply from this attempt at all, so a
    change PREPARED here is stored SUPERSEDED. The retry runs the turn again, the same
    key or V13 returns the same hold, and the retry's own T1b records it PENDING with
    the model's new wording (plan section 5.11, row 7).
    """
    if run is None or run.result.booking_outcome is None:
        return
    async with context.sessionmaker() as session:
        # FIRST, before anything writes.
        await ConversationRepository(session, context.tenant_id).current_state(
            conversation_id, for_update=True
        )
        await _record_run(
            session,
            context,
            run,
            inbound_id=inbound_id,
            conversation_id=conversation_id,
            # No reply: this attempt is being retried, and nothing was reserved.
            reply_message_id=None,
        )
        await _apply_booking_outcome(
            session,
            context,
            run,
            conversation_id=conversation_id,
            inbound_id=inbound_id,
            confirmable=False,
        )
        await session.commit()


async def handle_message(context: EventContext) -> str:
    """Store the inbound message, then answer it exactly once - with the model's
    reply (VS-005), generated with no transaction open.

    Requirement 2 of VS-004 still holds: EVERY inbound type is stored; only the
    types in WHATSAPP_REPLY_TO_TYPES get a reply.

    The order below is the correctness of the slice - see "Commit boundaries" in
    docs/plans/VS-005-plan.md before moving anything. In short: T1 stores and
    reads and is committed and CLOSED; the model runs outside any transaction;
    T1b re-reads the state (hard rule 7) and reserves the reply WITH its text;
    Meta is sent the STORED text; T2 records the wamid.

    Every log line uses event_id=<webhook_inbox row uuid> (plan note C2). The
    wamid is stored in messages.provider_message_id and never logged: it is
    base64 and decodes to include the patient's phone number.
    """
    message = _validated_message(context.item)
    wa_id = message.from_
    if not wa_id:
        # No sender means no contact, no conversation and nobody to reply to.
        raise PermanentJobError("unmodelled_message")

    reply_wanted = (message.type or "") in context.settings.reply_to_types
    body = message.text.get("body") if isinstance(message.text, dict) else None

    # --- T1 ---------------------------------------------------------------
    async with context.sessionmaker() as session:
        await WebhookInboxRepository(session).attach_tenant(context.event_id, context.tenant_id)

        contacts = ContactRepository(session, context.tenant_id)
        contact = await contacts.get_or_create_by_identity(
            Channel.WHATSAPP, wa_id, display_name=_display_name_for(context.payload, wa_id)
        )

        conversations = ConversationRepository(session, context.tenant_id)
        try:
            conversation = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
        except IntegrityError:
            # VS-002's follow-up, pulled in by requirement 2. Another worker won
            # the race between the check and the insert; a retry re-reads and
            # finds their conversation. Never dead-lettered, and the original
            # exception is not chained - the constraint name is the most that
            # should ever reach a log.
            raise RetryableJobError("conversation_race") from None

        messages = MessageRepository(session, context.tenant_id)
        try:
            inbound = await messages.add(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                modality=_modality_for(message.type),
                status=MessageStatus.RECEIVED,
                text=body,
                provider_message_id=message.id,
            )
        except DuplicateRecordError:
            # Not a failure: a previous try already stored it. This is what makes
            # the whole job re-runnable, and it only works because add() wraps its
            # INSERT in a savepoint (plan amendment A2) - without one the failed
            # INSERT would have aborted this transaction and the re-read below
            # would raise InFailedSqlTransaction.
            existing = await messages.get_by_provider_id(message.id)
            if existing is None:  # pragma: no cover - the conflict proves it exists
                raise RetryableJobError("inbound_message_vanished") from None
            inbound = existing

        conversation_id = conversation.id
        inbound_id = inbound.id

        if not reply_wanted:
            await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
            await session.commit()
            logger.info(
                "inbound stored without a reply event_id=%s type=%s",
                context.event_id,
                message.type,
            )
            return "stored_no_reply"

        reserved = await messages.get_reply_to(inbound_id)
        if reserved is not None and reserved.provider_message_id:
            # A previous try already sent it. Requirement 3: do not send again.
            await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
            await session.commit()
            logger.info("reply already sent event_id=%s", context.event_id)
            return "already_replied"

        # Hard rule 7, FIRST read. NOT the one that protects the send - that is
        # in T1b, after the model. This one keeps a conversation a human already
        # holds from costing a model call, and from sending the patient's words
        # to OpenAI for nothing (plan conflict S4).
        #
        # current_state() selects the state COLUMN, not the entity, and that is
        # the whole point. A select() for a mapped Conversation in this session is
        # resolved through SQLAlchemy's identity map and hands back the instance
        # get_or_create_open already loaded, with the state it had then - so the
        # "re-read" would never see a commit made by anyone else, which is the
        # only thing it exists to see.
        state = await conversations.current_state(conversation_id)
        if state is None or state not in _AI_STATES:
            return await _drop(session, context, inbound_id, conversation_id)

        turn: Turn | None = None
        if reserved is None:
            earlier = await messages.history_before(
                conversation_id, inbound_id, context.settings.agent_history_messages
            )
            # VS-007. Two reads, in this order.
            #
            # First, lapse any hold whose moment has passed, on the INJECTED clock -
            # never SQL now(). The expiry came from the Booking Service's clock, so
            # it must be compared with the clock the rest of the turn uses; message
            # ordering in the gate uses PostgreSQL's, and the two are never compared
            # with each other (plan risk R5).
            actions = BookingActionRepository(session, context.tenant_id)
            await actions.expire_pending(conversation_id, now=context.clock())
            # Then the gate's verdict, computed in SQL: is the latest prepared change
            # still PENDING, prepared by a DIFFERENT message, and was a reply of ours
            # actually SENT in between? The tools get a boolean and cannot
            # half-apply the rule (V3, hard rule 5).
            found = await actions.state_for(conversation_id, inbound_id)
            booking_state = (
                None
                if found is None
                else BookingState(
                    action_id=found.action_id,
                    kind=BookingActionKind(found.kind),
                    status=BookingActionStatus(found.status),
                    confirmable=found.confirmable,
                    hold_id=found.hold_id,
                    appointment_id=found.appointment_id,
                )
            )
            # The patient reference the Booking Service asked for: this contact's
            # STORED WhatsApp number (V14 as the developer overrode it). Read from
            # our own row rather than taken off the payload, so the value sent is the
            # value we store - the same on every retry. It is never sent to the
            # model, never logged, and in no table, schema, result or dead letter.
            patient_reference = await contacts.external_id(contact.id, Channel.WHATSAPP)
            turn = Turn(
                tenant_id=context.tenant_id,
                contact_id=contact.id,
                conversation_id=conversation_id,
                modality=MessageModality(inbound.modality),
                input_text=inbound.text,
                history=tuple(_history_entry(row) for row in earlier),
                inbox_event_id=context.event_id,
                inbound_message_id=inbound_id,
                booking_state=booking_state,
                patient_reference=patient_reference,
            )
        await session.commit()
    # The session is CLOSED. No transaction is open from here until T1b: the
    # conversation row lock MessageRepository.add took (last_inbound_at) was
    # released by that commit, so a staff member taking over never waits for
    # OpenAI. Outside the block, not merely after the commit, so no stray query
    # can open a transaction that then stays open across the call.

    # --- generation: only when no reply row exists yet -----------------------
    reply_text: str | None = None  # None: send the text an earlier try reserved
    failure: str | None = None  # set: the fallback is the reply, and a dead letter is owed
    run: GeneratedRun | None = None  # set only when generation ran on THIS try
    if turn is not None:
        started = time.monotonic()
        generated = await process_turn(
            turn,
            context.chat,
            AgentRuntime(
                booking=context.booking,
                clock=context.clock,
                turn_timeout_seconds=context.settings.agent_turn_timeout_seconds,
                patient_bookings=context.patient_bookings,
            ),
        )
        run = GeneratedRun(
            result=generated,
            # Measured around process_turn, so it INCLUDES the tool calls: what
            # the patient actually waited for.
            duration_ms=int((time.monotonic() - started) * 1000),
            job_try=context.job_try,
        )
        # Codes and counts only. No tool NAMES (the same reasoning as Q9: an
        # unknown one is model-written), no arguments, no results, no text.
        logger.info(
            "reply generated event_id=%s outcome=%s reason=%s prompt_version=%s "
            "history=%d model_calls=%d tool_calls=%d tool_errors=%d "
            "prompt_tokens=%s completion_tokens=%s duration_ms=%d",
            context.event_id,
            generated.outcome.value,
            generated.reason,
            generated.prompt_version,
            len(turn.history),
            generated.model_calls,
            len(generated.tool_calls),
            sum(
                1 for record in generated.tool_calls if record.status is not ToolExecutionStatus.OK
            ),
            generated.prompt_tokens,
            generated.completion_tokens,
            run.duration_ms,
        )
        if generated.outcome is ChatOutcome.SUCCESS:
            reply_text = generated.reply_text
        elif (
            generated.outcome is ChatOutcome.RETRYABLE
            and context.job_try < context.settings.job_max_tries
            # V11. A turn that already EXECUTED a change is never generated again,
            # however it then failed. A re-run would bill a second time and let a
            # fresh model run decide something else - hold another slot after a
            # booking - so the patient's answer would depend on a second model run
            # instead of on the fact we already have. The fallback plus the receipt
            # tells them what happened.
            and not _final_change(generated)
        ):
            # V9. The attempt made a booking CHANGE, so it is recorded before the
            # retry rather than lost with it: its model calls were billed, its tools
            # ran, and the Booking Service may now hold a hold nothing of ours knows
            # about. Read-only attempts keep Q1 and record nothing.
            if generated.booking_outcome is not None:
                await _record_attempt(
                    context, run, conversation_id=conversation_id, inbound_id=inbound_id
                )
            # Nothing is reserved, so the next try starts clean and asks again.
            raise RetryableJobError(generated.reason)
        else:
            # Permanent, or out of tries (requirement 4): the fallback goes out
            # through the SAME exactly-once path as any reply, and a human still
            # hears about the failure.
            reply_text = context.settings.agent_fallback_reply
            failure = generated.reason

    # --- T1b -----------------------------------------------------------------
    async with context.sessionmaker() as session:
        conversations = ConversationRepository(session, context.tenant_id)
        messages = MessageRepository(session, context.tenant_id)
        # Hard rule 7, SECOND and authoritative read: after the model, in a new
        # transaction, immediately before the reservation and the send. The model
        # call is the longest thing the job does, so the check that protects the
        # send has to come after it.
        # FOR UPDATE when the turn carries a booking outcome, and FIRST in the
        # transaction. Two quick messages can each produce a turn with an outcome;
        # without the lock both would supersede the same PENDING row and both insert
        # a new one, the second violating the partial unique index. The reply's own
        # insert takes a KEY SHARE lock on this row through its foreign key, so a
        # lock taken after it could deadlock two T1b's (plan check U8).
        carries_outcome = run is not None and run.result.booking_outcome is not None
        state = await conversations.current_state(conversation_id, for_update=carries_outcome)
        if state is None or state not in _AI_STATES:
            return await _drop(
                session,
                context,
                inbound_id,
                conversation_id,
                failure,
                run=run,
                second_read=True,
            )

        # G1's rule for WHICH receipt is shown. An EXECUTED success's receipt always
        # goes, whatever the reply is - the change happened and the patient is owed
        # the proof. A PREPARED change's receipt goes only with the model's OWN reply,
        # because the ⏳ refers to a question only the model's wording asked.
        receipt: str | None = None
        outcome = run.result.booking_outcome if run is not None else None
        if outcome is not None and outcome.receipt:
            executed_success = (
                outcome.phase is ChangePhase.EXECUTED and outcome.status is ChangeStatus.SUCCESS
            )
            if executed_success or failure is None:
                receipt = outcome.receipt
        if reply_text is not None:
            reply_text = compose_reply(reply_text, receipt)

        if reply_text is not None:
            # WITH the text. Once a text is reserved, that text IS the reply:
            # ON CONFLICT DO NOTHING returns whichever row the database kept,
            # and it is that row's text that gets sent.
            reply = await messages.reserve_reply(conversation_id, inbound_id, reply_text)
        else:
            reply = await messages.get_reply_to(inbound_id)
            if reply is None:  # pragma: no cover - T1 saw it, and nothing deletes it
                raise RetryableJobError("reply_row_vanished")

        if reply.provider_message_id:  # pragma: no cover - the lease makes this unreachable
            await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
            await session.commit()
            logger.info("reply already sent event_id=%s", context.event_id)
            return "already_replied"

        reply_id, text_to_send = reply.id, reply.text
        if failure is not None:
            # In THIS transaction, with the reservation (plan conflict C7). It carries
            # the booking block too: somebody triaging a fallback - or an
            # `agent_unconfirmed_claim` - needs to know whether a change happened
            # before they reply to the patient by hand.
            await _record_generation_failure(session, context, failure, booking=outcome)
        # AFTER the reservation, inside its own SAVEPOINT: if this fails, only
        # these rows roll back and the reply still goes out.
        await _record_run(
            session,
            context,
            run,
            inbound_id=inbound_id,
            conversation_id=conversation_id,
            reply_message_id=reply_id,
        )
        # Then the booking outcome, also in a SAVEPOINT. `confirmable` is "will the
        # patient see this turn's OWN wording?": the model's reply, yes; the fallback
        # or a guard replacement, no - and a change prepared in such a turn is stored
        # SUPERSEDED, because nothing described it to the patient (plan section 5.4).
        await _apply_booking_outcome(
            session,
            context,
            run,
            conversation_id=conversation_id,
            inbound_id=inbound_id,
            confirmable=failure is None,
        )
        # COMMIT before touching Meta. A reply row written in the same
        # transaction as the wamid would leave no trace of an attempted send, and
        # the retry would have nothing to recognise.
        await session.commit()

    # --- the one Meta call: always the STORED text (requirement 2) ------------
    result = await context.meta.send_text(context.phone_number_id, wa_id, text_to_send)

    if result.outcome is SendOutcome.RETRYABLE:
        if context.job_try >= context.settings.job_max_tries:
            # The envelope is about to dead-letter this event, so no later try
            # will ever send this row. Left QUEUED, it would reach later prompts
            # as something the clinic said (plan conflict C9).
            async with context.sessionmaker() as session:
                await MessageRepository(session, context.tenant_id).mark_failed(reply_id)
                await session.commit()
        # Otherwise the row stays QUEUED with no wamid, so the next try
        # recognises it and sends its STORED text again - never a second model
        # call.
        raise RetryableJobError(result.reason)

    if result.outcome is SendOutcome.PERMANENT:
        async with context.sessionmaker() as session:
            await MessageRepository(session, context.tenant_id).mark_failed(reply_id)
            await session.commit()
        # Hard rule 5's shape: nothing is claimed to have been sent.
        raise PermanentJobError(result.reason)

    # --- T2 -----------------------------------------------------------------
    async with context.sessionmaker() as session:
        messages = MessageRepository(session, context.tenant_id)
        if result.provider_message_id:
            await messages.attach_provider_id(reply_id, result.provider_message_id)
        else:
            # accepted_without_id: Meta took the message and we cannot read the
            # id it gave it. SENT, and never resent - see the client, and the
            # plan's duplicate-reply gap.
            await messages.mark_sent_without_id(reply_id)
        await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
        await session.commit()

    logger.info("replied event_id=%s conversation_id=%s", context.event_id, conversation_id)
    if not result.provider_message_id:
        return "sent_without_id"
    # replied_fallback, not replied: the patient WAS answered, and the dead
    # letter written above says why it was not an AI reply.
    return "replied_fallback" if failure is not None else "replied"


# Meta's status words, mapped to our vocabulary. Anything not in here is ignored
# rather than dead-lettered (plan assumption A12): Meta adds status values without
# notice, and a dead letter per unrecognised word would fill a triage table with
# things nobody will ever act on.
_STATUS_WORDS: dict[str, MessageStatus] = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


def _validated_status(item: Any) -> StatusUpdate:
    """The item as a status callback, or a permanent failure."""
    if not isinstance(item, dict):
        raise PermanentJobError("unmodelled_status")
    try:
        return StatusUpdate.model_validate(item)
    except ValidationError:
        raise PermanentJobError("unmodelled_status") from None


def _failure_codes(item: Any) -> str:
    """Meta's error codes from a `failed` callback, sanitised.

    Codes only, never `error.message` or `error.title`: those are written for a
    human and quote the recipient's number for the most common failure there is
    (hard rule 8, requirement 6).
    """
    errors = item.get("errors") if isinstance(item, dict) else None
    if not isinstance(errors, list):
        return ""
    codes = [
        f"code_{entry['code']}"
        for entry in errors
        if isinstance(entry, dict) and isinstance(entry.get("code"), int)
    ]
    return scrub(" ".join(codes))


async def handle_status(context: EventContext) -> str:
    """Advance one outbound message's delivery status.

    Look the message up FIRST, then advance. The lookup is not redundant with
    advance_status's return value - they answer different questions. "No such
    wamid" means the status arrived before the worker saved it, which is Meta's
    ordinary out-of-order delivery and is retryable. "Found, but did not move"
    means the status was old or repeated, and is a success. One boolean cannot
    distinguish those, and treating them alike would either lose a real status or
    dead-letter a duplicate one.

    The wamid is read out of the payload and never logged (plan note C2): here it
    is the id of a message WE sent TO the patient, so it identifies them twice
    over. Log lines carry event_id, the status word, and whether the row moved.
    """
    status = _validated_status(context.item)
    target = _STATUS_WORDS.get(status.status.lower())

    async with context.sessionmaker() as session:
        inbox = WebhookInboxRepository(session)
        await inbox.attach_tenant(context.event_id, context.tenant_id)

        if target is None:
            # A status value we do not model. PROCESSED, not dead-lettered (A12).
            await inbox.mark(context.event_id, InboxStatus.PROCESSED)
            await session.commit()
            logger.info("status ignored event_id=%s status=%s", context.event_id, status.status)
            return "status_ignored"

        messages = MessageRepository(session, context.tenant_id)
        if await messages.get_by_provider_id(status.id) is None:
            # Requirement 5: the status webhook can arrive before the worker has
            # saved the wamid. Retryable, and the commit above is discarded with
            # the rollback - attach_tenant will run again on the next try.
            #
            # Assumption A11's consequence: a status for a message we never sent
            # retries through the whole curve and then dead-letters as
            # status_before_wamid. Noisy but honest.
            raise RetryableJobError("status_before_wamid")

        moved = await messages.advance_status(status.id, target)
        await inbox.mark(context.event_id, InboxStatus.PROCESSED)
        await session.commit()

    if target is MessageStatus.FAILED:
        codes = _failure_codes(context.item)
        logger.warning(
            "outbound message failed event_id=%s moved=%s codes=%s",
            context.event_id,
            moved,
            codes,
        )
    else:
        logger.info(
            "status advanced event_id=%s status=%s moved=%s",
            context.event_id,
            target.value,
            moved,
        )
    return "status_advanced" if moved else "status_not_moved"
