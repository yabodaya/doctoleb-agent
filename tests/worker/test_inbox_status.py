"""Status callbacks: forward only, and the one that arrives too early."""

import logging

import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.db.enums import InboxStatus, MessageDirection, MessageStatus
from app.db.models import DeadLetterJob, Message, WebhookInbox
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.whatsapp_factories import PATIENT_TEXT, phone, wamid
from tests.worker.conftest import (
    Meta,
    job_context,
    message_payload,
    meta_client,
    ok_response,
    status_payload,
    store_event,
    worker_settings,
)

pytestmark = pytest.mark.db

REPLY_WAMID = wamid(9)


async def _send_a_reply(sessionmaker) -> None:
    """Drive a message job so there is an outbound row with REPLY_WAMID on it."""
    settings = worker_settings()
    event_id = await store_event(sessionmaker, message_payload(1), 1)
    await process_inbox_event(
        job_context(sessionmaker, meta_client(Meta(ok_response(9)), settings), settings),
        str(event_id),
    )


async def _run_status(sessionmaker, n=2, state="sent", settings=None, **ctx_overrides):
    settings = settings or worker_settings()
    payload = status_payload(9, state)
    event_id = await store_event(sessionmaker, payload, n)
    outcome = await process_inbox_event(
        job_context(sessionmaker, meta_client(Meta(), settings), settings, **ctx_overrides),
        str(event_id),
    )
    return event_id, outcome


async def _reply_status(sessionmaker) -> str:
    async with sessionmaker() as session:
        row = await session.scalar(
            sa.select(Message).where(Message.direction == MessageDirection.OUTBOUND.value)
        )
        return row.status


async def _inbox(sessionmaker, event_id):
    async with sessionmaker() as session:
        return await session.scalar(sa.select(WebhookInbox).where(WebhookInbox.id == event_id))


async def test_a_sent_callback_marks_the_reply_sent(sessionmaker_for):
    await _send_a_reply(sessionmaker_for)

    event_id, outcome = await _run_status(sessionmaker_for, state="sent")

    # attach_provider_id already set SENT, so this callback is a no-op that
    # succeeds - which is the correct answer, not an error.
    assert outcome == "status_not_moved"
    assert await _reply_status(sessionmaker_for) == MessageStatus.SENT.value
    assert (await _inbox(sessionmaker_for, event_id)).status == InboxStatus.PROCESSED.value


async def test_delivered_and_read_advance_in_order(sessionmaker_for):
    await _send_a_reply(sessionmaker_for)

    _, first = await _run_status(sessionmaker_for, n=2, state="delivered")
    assert first == "status_advanced"
    assert await _reply_status(sessionmaker_for) == MessageStatus.DELIVERED.value

    _, second = await _run_status(sessionmaker_for, n=3, state="read")
    assert second == "status_advanced"
    assert await _reply_status(sessionmaker_for) == MessageStatus.READ.value


async def test_a_delivered_callback_after_read_changes_nothing(sessionmaker_for):
    """Requirement 5's "never read -> delivered".

    Meta delivers out of order routinely, so this is the ordinary case, not an
    error: the job succeeds and the row does not move.
    """
    await _send_a_reply(sessionmaker_for)
    await _run_status(sessionmaker_for, n=2, state="read")

    _, outcome = await _run_status(sessionmaker_for, n=3, state="delivered")

    assert outcome == "status_not_moved"
    assert await _reply_status(sessionmaker_for) == MessageStatus.READ.value


async def test_a_repeated_read_callback_changes_nothing(sessionmaker_for):
    await _send_a_reply(sessionmaker_for)
    await _run_status(sessionmaker_for, n=2, state="read")

    _, outcome = await _run_status(sessionmaker_for, n=3, state="read")

    assert outcome == "status_not_moved"
    assert await _reply_status(sessionmaker_for) == MessageStatus.READ.value


async def test_a_failed_callback_overwrites_sent(sessionmaker_for):
    """The FAILED rank decision: a send Meta later reports as failed must win
    over SENT."""
    await _send_a_reply(sessionmaker_for)

    _, outcome = await _run_status(sessionmaker_for, n=2, state="failed")

    assert outcome == "status_advanced"
    assert await _reply_status(sessionmaker_for) == MessageStatus.FAILED.value


async def test_a_failed_callback_does_not_overwrite_delivered(sessionmaker_for):
    """The other half of it: a message that was delivered did not fail."""
    await _send_a_reply(sessionmaker_for)
    await _run_status(sessionmaker_for, n=2, state="delivered")

    _, outcome = await _run_status(sessionmaker_for, n=3, state="failed")

    assert outcome == "status_not_moved"
    assert await _reply_status(sessionmaker_for) == MessageStatus.DELIVERED.value


async def test_a_status_for_an_unknown_wamid_is_retryable(sessionmaker_for):
    """Requirement 5's status-before-wamid case.

    Meta can deliver the `sent` callback before our worker has finished writing
    the wamid. That is out-of-order delivery, not an error, so it must not
    dead-letter on the first try.
    """
    with pytest.raises(Retry):
        await _run_status(sessionmaker_for, state="delivered")

    assert await _all_dead_letters(sessionmaker_for) == []


async def _all_dead_letters(sessionmaker):
    async with sessionmaker() as session:
        return list((await session.scalars(sa.select(DeadLetterJob))).all())


async def test_the_same_status_job_succeeds_once_the_wamid_is_stored(sessionmaker_for):
    """Proves the retry is useful, not merely tolerated."""
    settings = worker_settings()
    status_event = await store_event(sessionmaker_for, status_payload(9, "delivered"), 2)
    ctx = job_context(sessionmaker_for, meta_client(Meta(), settings), settings)

    with pytest.raises(Retry):
        await process_inbox_event(ctx, str(status_event))

    await _send_a_reply(sessionmaker_for)

    assert await process_inbox_event(ctx, str(status_event)) == "status_advanced"
    assert await _reply_status(sessionmaker_for) == MessageStatus.DELIVERED.value


async def test_a_status_for_an_unknown_wamid_dead_letters_after_max_tries(sessionmaker_for):
    """Plan assumption A11's consequence, stated as a test so it is not a
    surprise in production: a status for a message we never sent - one a staff
    member sent from the Meta Business app - retries the whole curve and then
    lands in dead_letter_jobs."""
    settings = worker_settings()

    _, outcome = await _run_status(
        sessionmaker_for, state="read", settings=settings, job_try=settings.job_max_tries
    )

    assert outcome == "dead_lettered"
    letters = await _all_dead_letters(sessionmaker_for)
    assert len(letters) == 1
    assert letters[0].error == "status_before_wamid"
    assert letters[0].payload["kind"] == "status"


async def test_a_status_word_we_do_not_model_is_ignored_not_dead_lettered(sessionmaker_for):
    """Plan assumption A12. Meta adds status values (deleted, warning) without
    notice, and a dead letter for each would fill a triage table with noise."""
    await _send_a_reply(sessionmaker_for)

    event_id, outcome = await _run_status(sessionmaker_for, n=2, state="deleted")

    assert outcome == "status_ignored"
    assert (await _inbox(sessionmaker_for, event_id)).status == InboxStatus.PROCESSED.value
    assert await _all_dead_letters(sessionmaker_for) == []


async def test_a_status_item_that_fails_its_model_dead_letters(sessionmaker_for):
    payload = status_payload(9, "sent")
    payload["item"] = {"recipient_id": phone(1)}  # no id, no status

    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, payload, 2)
    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta(), settings), settings), str(event_id)
    )

    assert outcome == "dead_lettered"
    assert (await _all_dead_letters(sessionmaker_for))[0].error == "unmodelled_status"


async def test_a_status_for_another_tenants_message_does_not_advance_it(sessionmaker_for):
    """MessageRepository is tenant-scoped; this proves the scoping is not
    bypassed by the status path (hard rule 4)."""
    await _send_a_reply(sessionmaker_for)
    # The same phone_number_id mapped to a DIFFERENT tenant.
    settings = worker_settings(whatsapp_tenant_map=f'{{"100000000000001": "{dbf.TENANT_B}"}}')

    with pytest.raises(Retry):
        await _run_status(sessionmaker_for, n=2, state="read", settings=settings)

    assert await _reply_status(sessionmaker_for) == MessageStatus.SENT.value


async def test_a_failed_callback_logs_the_meta_error_code_and_nothing_else(
    sessionmaker_for, caplog
):
    await _send_a_reply(sessionmaker_for)
    payload = status_payload(9, "failed")
    payload["item"]["errors"] = [
        {
            "code": 131050,
            "title": "Unable to deliver message",
            "message": f"Recipient {phone(1)} has not accepted our new Terms",
            "error_data": {"details": f"number {phone(1)}"},
        }
    ]
    settings = worker_settings()
    event_id = await store_event(sessionmaker_for, payload, 2)

    with caplog.at_level(logging.DEBUG):
        await process_inbox_event(
            job_context(sessionmaker_for, meta_client(Meta(), settings), settings), str(event_id)
        )

    assert "code_131050" in caplog.text
    assert "Unable to deliver" not in caplog.text
    assert phone(1) not in caplog.text


async def test_no_status_log_line_contains_the_wamid_it_is_about(sessionmaker_for, caplog):
    """Plan note C2, and the status handler is where it is most tempting.

    The obvious debug line is "status X for wamid Y", and that wamid is the id of
    a message WE sent TO the patient - so it identifies them twice over.
    """
    await _send_a_reply(sessionmaker_for)

    with caplog.at_level(logging.DEBUG):
        event_id, _ = await _run_status(sessionmaker_for, n=2, state="delivered")

    assert str(event_id) in caplog.text
    for value in (REPLY_WAMID, wamid(1), phone(1), PATIENT_TEXT):
        assert value not in caplog.text
