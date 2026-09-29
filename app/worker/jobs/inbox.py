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
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import HistoryEntry, Turn, process_turn
from app.channels.whatsapp.client import MetaClient, SendOutcome
from app.channels.whatsapp.payloads import InboundMessage, InboxItemKind, StatusUpdate
from app.channels.whatsapp.redact import scrub
from app.config import Settings
from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Message
from app.db.repositories import (
    ContactRepository,
    ConversationRepository,
    DeadLetterJobRepository,
    MessageRepository,
    WebhookInboxRepository,
)
from app.db.repositories.errors import DuplicateRecordError
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
    session: AsyncSession, context: EventContext, reason: str
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
        ),
        error=reason,
        attempts=context.job_try,
        tenant_id=context.tenant_id,
        source_event_id=str(context.event_id),
    )


async def _drop(
    session: AsyncSession,
    context: EventContext,
    inbound_id: uuid.UUID,
    conversation_id: uuid.UUID,
    failure: str | None = None,
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
    if failure is not None:
        await _record_generation_failure(session, context, failure)
    await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
    await session.commit()
    # Ids only, never the message (hard rule 7's own wording).
    logger.info(
        "reply dropped, conversation not AI-active event_id=%s conversation_id=%s",
        context.event_id,
        conversation_id,
    )
    return "dropped_not_ai_active"


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
            turn = Turn(
                tenant_id=context.tenant_id,
                contact_id=contact.id,
                conversation_id=conversation_id,
                modality=MessageModality(inbound.modality),
                input_text=inbound.text,
                history=tuple(_history_entry(row) for row in earlier),
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
    if turn is not None:
        generated = await process_turn(turn, context.chat)
        logger.info(
            "reply generated event_id=%s outcome=%s reason=%s prompt_version=%s "
            "history=%d prompt_tokens=%s completion_tokens=%s",
            context.event_id,
            generated.outcome.value,
            generated.reason,
            generated.prompt_version,
            len(turn.history),
            generated.prompt_tokens,
            generated.completion_tokens,
        )
        if generated.outcome is ChatOutcome.SUCCESS:
            reply_text = generated.reply_text
        elif (
            generated.outcome is ChatOutcome.RETRYABLE
            and context.job_try < context.settings.job_max_tries
        ):
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
        state = await conversations.current_state(conversation_id)
        if state is None or state not in _AI_STATES:
            return await _drop(session, context, inbound_id, conversation_id, failure)

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
            # In THIS transaction, with the reservation (plan conflict C7).
            await _record_generation_failure(session, context, failure)
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
