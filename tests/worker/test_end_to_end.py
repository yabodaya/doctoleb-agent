"""Webhook in, reply out. The acceptance criteria that span components.

The webhook is driven over HTTP with a recording queue; the jobs are then run
with the row ids that queue collected, which is exactly what arq would do. Meta
is always a MockTransport.
"""

import asyncio
import logging
import time

import httpx
import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.config import Settings, get_settings
from app.db.enums import InboxStatus, MessageDirection, MessageStatus
from app.db.models import DeadLetterJob, Message, WebhookInbox
from app.db.session import get_session
from app.main import create_app
from app.queue import get_job_queue
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.queue.fakes import FakeJobQueue
from tests.whatsapp_factories import (
    APP_SECRET,
    PATIENT_TEXT,
    PROFILE_NAME,
    VERIFY_TOKEN,
    envelope,
    phone,
    signed,
    status_update,
    text_message,
    wamid,
)
from tests.worker.conftest import (
    ACCESS_TOKEN,
    Meta,
    job_context,
    meta_client,
    ok_response,
)

pytestmark = pytest.mark.db

PATH = "/webhooks/whatsapp"


def app_settings(**overrides) -> Settings:
    """Settings for the whole path: real Meta fakes, and a mapped tenant."""
    values = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "meta_app_secret": APP_SECRET,
        "meta_verify_token": VERIFY_TOKEN,
        "meta_access_token": ACCESS_TOKEN,
        "meta_api_version": "v21.0",
        "meta_api_base_url": "https://graph.facebook.com",
        "whatsapp_tenant_map": f'{{"100000000000001": "{dbf.TENANT_A}"}}',
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def pipeline(sessionmaker_for, client_for):
    """A webhook client and a queue, both wired to the committing sessionmaker.

    Deliberately NOT the `use_database` fixture: the whole point of these tests
    is that the worker reads in a different transaction from the one the webhook
    wrote in, which the rollback-wrapped session cannot express.
    """
    settings = app_settings()
    app = create_app(settings)
    queue = FakeJobQueue()

    async def session_override():
        async with sessionmaker_for() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_job_queue] = lambda: queue

    class Pipeline:
        def __init__(self):
            self.settings = settings
            self.queue = queue
            self.transport: Meta | None = None

        async def post(self, body):
            raw, headers = signed(body, secret=APP_SECRET)
            async with client_for(app) as client:
                return await client.post(PATH, content=raw, headers=headers)

        async def drain(self, transport: Meta, expect_retry: bool = False):
            """Run a job for every id the webhook enqueued, in order."""
            ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)
            outcomes = []
            while self.queue.enqueued:
                row_id = self.queue.enqueued.pop(0)
                try:
                    outcomes.append(await process_inbox_event(ctx, str(row_id)))
                except Retry:
                    if not expect_retry:
                        raise
                    outcomes.append("retry")
            return outcomes

    return Pipeline()


async def _rows(sessionmaker, model, **where):
    async with sessionmaker() as session:
        statement = sa.select(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        return list((await session.scalars(statement)).all())


async def test_a_webhook_delivery_becomes_one_stored_message_and_one_reply(
    sessionmaker_for, pipeline
):
    """The slice's headline, minus the phone."""
    transport = Meta(ok_response(9))

    assert (await pipeline.post(envelope(messages=[text_message(1)]))).status_code == 200
    assert await pipeline.drain(transport) == ["replied"]

    assert transport.sends == 1
    inbound = await _rows(sessionmaker_for, Message, direction=MessageDirection.INBOUND.value)
    reply = await _rows(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value)
    assert len(inbound) == 1 and len(reply) == 1
    assert reply[0].provider_message_id == wamid(9)
    assert reply[0].reply_to_message_id == inbound[0].id
    inbox = await _rows(sessionmaker_for, WebhookInbox)
    assert [row.status for row in inbox] == [InboxStatus.PROCESSED.value]
    assert inbox[0].tenant_id == dbf.TENANT_A


async def test_the_same_webhook_delivered_twice_produces_one_message_and_one_reply(
    sessionmaker_for, pipeline
):
    """Requirement 7's dedup case, end to end.

    Both deliveries enqueue - the second on purpose (plan note C8) - and both jobs
    run. One inbound row, one reply, one send.
    """
    transport = Meta(ok_response(9))
    body = envelope(messages=[text_message(1)])

    await pipeline.post(body)
    await pipeline.post(body)
    outcomes = await pipeline.drain(transport)

    assert outcomes == ["replied", "skipped"]
    assert transport.sends == 1
    assert len(await _rows(sessionmaker_for, WebhookInbox)) == 1
    assert len(await _rows(sessionmaker_for, Message)) == 2  # one inbound, one reply


async def test_the_webhook_answers_fast_when_the_meta_send_is_slow(sessionmaker_for, pipeline):
    """The slice's second acceptance criterion, and Review Focus 1.

    The transport sleeps for two seconds. The webhook must not care, because the
    webhook never calls Meta - so the request is timed AND the transport is
    asserted untouched during it.
    """

    async def stall(request):
        await asyncio.sleep(2)

    transport = Meta(hook=stall)

    started = time.perf_counter()
    response = await pipeline.post(envelope(messages=[text_message(1)]))
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert transport.sends == 0, "the webhook called Meta"
    assert elapsed < 1.0, f"the webhook took {elapsed:.2f}s"

    # And the slow send does happen, on the far side of the queue.
    assert await pipeline.drain(transport) == ["replied"]
    assert transport.sends == 1


async def test_a_status_delivery_after_the_reply_advances_it_to_read(sessionmaker_for, pipeline):
    transport = Meta(ok_response(9))

    await pipeline.post(envelope(messages=[text_message(1)]))
    await pipeline.drain(transport)

    await pipeline.post(
        envelope(
            statuses=[
                status_update(9, "sent"),
                status_update(9, "delivered"),
                status_update(9, "read"),
            ]
        )
    )
    outcomes = await pipeline.drain(transport)

    assert "status_advanced" in outcomes
    reply = (await _rows(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value))[0]
    assert reply.status == MessageStatus.READ.value


async def test_a_status_delivered_before_the_reply_job_ran_retries_then_succeeds(
    sessionmaker_for, pipeline
):
    """The ordering acceptance case, in the order Meta can actually produce.

    Meta can deliver the `sent` callback for our reply before our own worker has
    finished writing the wamid. The status job must defer, not lose it.
    """
    transport = Meta(ok_response(9))

    # The status arrives first, and its job runs first.
    await pipeline.post(envelope(statuses=[status_update(9, "delivered")]))
    assert await pipeline.drain(transport, expect_retry=True) == ["retry"]

    # Then the message, which produces the wamid.
    await pipeline.post(envelope(messages=[text_message(1)]))
    assert await pipeline.drain(transport) == ["replied"]

    # And the status job, retried, now finds it.
    key = f"status:{wamid(9)}:delivered"
    status_row = (await _rows(sessionmaker_for, WebhookInbox, provider_event_id=key))[0]
    settings = pipeline.settings
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)
    assert await process_inbox_event(ctx, str(status_row.id)) == "status_advanced"

    reply = (await _rows(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value))[0]
    assert reply.status == MessageStatus.DELIVERED.value


async def test_a_meta_outage_retries_and_then_dead_letters_without_replying(
    sessionmaker_for, pipeline
):
    """Hard rule 11 and hard rule 5's shape together.

    Five tries, all 500. One dead letter, no wamid anywhere, no lease left behind,
    and the patient told nothing untrue.
    """
    transport = Meta(httpx.Response(500, json={"error": {"code": 1}}))
    await pipeline.post(envelope(messages=[text_message(1)]))
    row_id = pipeline.queue.enqueued[0]
    settings = pipeline.settings
    ctx = job_context(sessionmaker_for, meta_client(transport, settings), settings)

    outcomes = []
    for job_try in range(1, settings.job_max_tries + 1):
        ctx["job_try"] = job_try
        try:
            outcomes.append(await process_inbox_event(ctx, str(row_id)))
        except Retry:
            outcomes.append("retry")

    assert outcomes[:-1] == ["retry"] * (settings.job_max_tries - 1)
    assert outcomes[-1] == "dead_lettered"
    assert transport.sends == settings.job_max_tries

    letters = await _rows(sessionmaker_for, DeadLetterJob)
    assert len(letters) == 1
    assert letters[0].error == "http_500 code_1"
    assert letters[0].attempts == settings.job_max_tries

    reply = (await _rows(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value))[0]
    assert reply.provider_message_id is None
    assert reply.status == MessageStatus.QUEUED.value
    inbox = (await _rows(sessionmaker_for, WebhookInbox))[0]
    assert inbox.status == InboxStatus.FAILED.value
    assert inbox.locked_until is None, "a dead-lettered row must not stay leased"


async def test_nothing_in_redis_or_the_logs_from_a_full_run_contains_a_wamid(
    sessionmaker_for, pipeline, caplog
):
    """Plan note C2, once over the whole path, with nothing excluded.

    One test that fails if any log line anywhere on this path reaches for the
    obvious identifier. The webhook's own "stored" line used to be excluded by
    name - it printed provider_event_id values, inherited from VS-003 - and now
    logs row ids like everything else, so the exclusion is gone.
    """
    transport = Meta(ok_response(9))

    with caplog.at_level(logging.DEBUG):
        await pipeline.post(envelope(messages=[text_message(1)]))
        enqueued = list(pipeline.queue.enqueued)
        await pipeline.drain(transport)

    # Nothing patient-identifying reached Redis: one row id per job, no payload.
    assert all(str(row_id) for row_id in enqueued)
    for row_id in enqueued:
        assert wamid(1) not in str(row_id)

    every_line = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in (wamid(1), wamid(9), PATIENT_TEXT, PROFILE_NAME, phone(1), ACCESS_TOKEN):
        assert forbidden not in every_line
    # And the row ids ARE there, or the lines carry nothing to correlate with.
    for row_id in enqueued:
        assert str(row_id) in every_line
