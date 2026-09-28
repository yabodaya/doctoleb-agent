"""Webhook in, reply out. The acceptance criteria that span components.

The webhook is driven over HTTP with a recording queue; the jobs are then run
with the row ids that queue collected, which is exactly what arq would do. Meta
is always a MockTransport.
"""

import asyncio
import json
import logging
import time

import httpx
import httpx2
import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.config import Settings, get_settings
from app.db.enums import InboxStatus, MessageDirection, MessageStatus
from app.db.models import DeadLetterJob, Message, WebhookInbox
from app.db.session import get_session
from app.integrations.openai.chat import OpenAIChatClient
from app.main import create_app
from app.queue import get_job_queue
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as dbf
from tests.integrations.fakes import FakeChatClient, ok
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

        async def drain(self, transport: Meta, expect_retry: bool = False, **ctx_overrides):
            """Run a job for every id the webhook enqueued, in order.

            `chat` defaults to job_context's FakeChatClient. Tests whose point is
            the real classifier or the real request body pass an OpenAIChatClient
            built on an httpx2.MockTransport instead; `settings` overrides the
            job's settings without rebuilding the app, which only needs the Meta
            and tenant halves that are identical either way.
            """
            job_settings = ctx_overrides.pop("settings", settings)
            ctx = job_context(
                sessionmaker_for,
                meta_client(transport, job_settings),
                job_settings,
                **ctx_overrides,
            )
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

    VS-005 (plan conflict C9): the reply row ends FAILED, not QUEUED. No later
    try will ever send it, and a row left QUEUED would reach later prompts as
    something the clinic said - while also still claiming a reply is on its way.
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
    assert reply.status == MessageStatus.FAILED.value
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


# --- VS-005: the AI reply, end to end ---------------------------------------


def openai_settings(**overrides) -> Settings:
    """app_settings with an OpenAI account configured. Never a real key."""
    values = {"openai_api_key": "sk-test-not-a-real-one", "openai_chat_model": "test-model"}
    values.update(overrides)
    return app_settings(**values)


class OpenAI:
    """A recording httpx2 transport for the REAL OpenAIChatClient.

    The counterpart of `Meta`. Used where the point is what actually goes on the
    wire, or what the real classifier does with what comes back - neither of
    which a fake ChatClient can prove.
    """

    def __init__(self, *responses):
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def calls(self) -> int:
        return len(self.requests)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def completion(text: str) -> httpx2.Response:
    """A Chat Completions body in the shape the SDK parses."""
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1730000000,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
        },
    )


def real_chat(transport: OpenAI, settings: Settings) -> OpenAIChatClient:
    return OpenAIChatClient(
        settings, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
    )


async def test_a_webhook_delivery_becomes_one_ai_reply(sessionmaker_for, pipeline):
    """The slice's headline: the model's words reach the patient, unaltered.

    The real client, so the text asserted in Meta's request body is the text
    that came out of an OpenAI response body and through the real parser.
    """
    settings = openai_settings()
    openai = OpenAI(completion("Hello! How can the clinic help you today?"))
    meta = Meta(ok_response(9))

    assert (await pipeline.post(envelope(messages=[text_message(1)]))).status_code == 200
    outcomes = await pipeline.drain(meta, chat=real_chat(openai, settings), settings=settings)

    assert outcomes == ["replied"]
    assert openai.calls == 1
    body = json.loads(meta.requests[0].content)["text"]["body"]
    assert body == "Hello! How can the clinic help you today?"
    reply = (await _rows(sessionmaker_for, Message, direction=MessageDirection.OUTBOUND.value))[0]
    assert reply.text == "Hello! How can the clinic help you today?"


async def test_the_same_webhook_delivered_twice_calls_the_model_once_and_replies_once(
    sessionmaker_for, pipeline
):
    """Hard rule 2, now with a bill attached to getting it wrong."""
    settings = openai_settings()
    openai = OpenAI(completion("one reply only"))
    meta = Meta(ok_response(9))
    body = envelope(messages=[text_message(1)])

    await pipeline.post(body)
    await pipeline.post(body)
    outcomes = await pipeline.drain(meta, chat=real_chat(openai, settings), settings=settings)

    assert outcomes == ["replied", "skipped"]
    assert openai.calls == 1
    assert meta.sends == 1
    assert len(await _rows(sessionmaker_for, Message)) == 2


async def test_the_webhook_answers_fast_when_the_model_is_slow(pipeline):
    """Hard rule 1, with the new slow thing.

    The fake model sleeps for two seconds. The webhook must not care, because
    the webhook never calls a model - so the request is timed AND the fake is
    asserted untouched during it.
    """

    async def stall(messages):
        await asyncio.sleep(2)

    chat = FakeChatClient(ok(), hook=stall)

    started = time.monotonic()
    response = await pipeline.post(envelope(messages=[text_message(1)]))
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1
    assert chat.calls == []

    assert await pipeline.drain(Meta(ok_response(9)), chat=chat) == ["replied"]
    assert len(chat.calls) == 1


async def test_the_second_message_carries_the_first_exchange_to_the_model(pipeline):
    """Requirement 5, at the boundary where it is finally observable.

    Same patient, two messages: the second request must carry the first
    exchange as user/assistant turns, in order, with the new message last.
    """
    settings = openai_settings()
    openai = OpenAI(completion("first answer"), completion("second answer"))
    meta = Meta(ok_response(8), ok_response(9))

    await pipeline.post(envelope(messages=[text_message(1)]))
    await pipeline.drain(meta, chat=real_chat(openai, settings), settings=settings)
    await pipeline.post(
        envelope(messages=[text_message(1, body="and about the cost?", id=wamid(2))])
    )
    await pipeline.drain(meta, chat=real_chat(openai, settings), settings=settings)

    second = openai.bodies()[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[0]["content"].startswith("You are the WhatsApp receptionist")
    assert [m["content"] for m in second[1:]] == [
        PATIENT_TEXT,
        "first answer",
        "and about the cost?",
    ]


async def test_the_model_request_contains_no_ids_names_or_phone_numbers(sessionmaker_for, pipeline):
    """Hard rules 4 and 8, at the boundary where data leaves our systems.

    tenant_id is carried on the Turn for VS-006's tools and must never be sent;
    neither must the contact, conversation or inbox ids, the WhatsApp profile
    name, the patient's number, or a wamid - which is base64 and decodes to
    include that number.
    """
    settings = openai_settings()
    openai = OpenAI(completion("a reply"))

    await pipeline.post(envelope(messages=[text_message(1)]))
    await pipeline.drain(Meta(ok_response(9)), chat=real_chat(openai, settings), settings=settings)

    sent = openai.requests[0].content.decode("utf-8")
    forbidden = [str(dbf.TENANT_A), PROFILE_NAME, phone(1), wamid(1), wamid(9)]
    for model in (Message, WebhookInbox):
        for row in await _rows(sessionmaker_for, model):
            forbidden.append(str(row.id))
    for value in forbidden:
        assert value not in sent, value


async def test_an_openai_outage_retries_then_answers_with_the_fallback(sessionmaker_for, pipeline):
    """One retry layer, observed end to end: five tries, five requests.

    max_retries=0 is what makes those two numbers equal. At the SDK's default
    of 2 it would be fifteen requests, fifteen bills, and a dead letter saying
    five.
    """
    settings = openai_settings()
    openai = OpenAI(httpx2.Response(503, json={"error": {"message": "unavailable"}}))
    meta = Meta(ok_response(9))

    await pipeline.post(envelope(messages=[text_message(1)]))
    row_id = pipeline.queue.enqueued[0]
    ctx = job_context(
        sessionmaker_for,
        meta_client(meta, settings),
        settings,
        chat=real_chat(openai, settings),
    )

    outcomes = []
    for job_try in range(1, settings.job_max_tries + 1):
        ctx["job_try"] = job_try
        try:
            outcomes.append(await process_inbox_event(ctx, str(row_id)))
        except Retry:
            outcomes.append("retry")

    assert outcomes == ["retry"] * (settings.job_max_tries - 1) + ["replied_fallback"]
    assert openai.calls == settings.job_max_tries
    assert meta.sends == 1
    body = json.loads(meta.requests[0].content)["text"]["body"]
    assert body == settings.agent_fallback_reply
    letters = await _rows(sessionmaker_for, DeadLetterJob)
    assert [letter.error for letter in letters] == ["openai_http_503"]
    assert (await _rows(sessionmaker_for, WebhookInbox))[0].status == InboxStatus.PROCESSED.value


async def test_nothing_sensitive_reaches_logs_job_results_redis_or_dead_letters(
    sessionmaker_for, pipeline, caplog
):
    """Requirement 8, on every channel at once.

    One successful run and one no-credit run, through the REAL client, with a
    sentinel for each of the four things that must never escape: the patient's
    text, the model's reply, OpenAI's error message, and the key.
    """
    key = "sk-SENTINEL-key-not-a-real-one"
    settings = openai_settings(openai_api_key=key)
    patient = "SENTINEL-patient-text"
    reply = "SENTINEL-generated-reply"
    error_body = "SENTINEL-openai-error-message"

    healthy = OpenAI(completion(reply))
    no_credit = OpenAI(
        httpx2.Response(
            429,
            json={
                "error": {
                    "message": error_body,
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
        )
    )
    meta = Meta(ok_response(8), ok_response(9))
    enqueued: list = []

    with caplog.at_level(logging.DEBUG):
        await pipeline.post(envelope(messages=[text_message(1, body=patient)]))
        enqueued.extend(pipeline.queue.enqueued)
        first = await pipeline.drain(meta, chat=real_chat(healthy, settings), settings=settings)
        await pipeline.post(envelope(messages=[text_message(1, body=patient, id=wamid(2))]))
        enqueued.extend(pipeline.queue.enqueued)
        second = await pipeline.drain(meta, chat=real_chat(no_credit, settings), settings=settings)

    assert first == ["replied"]
    assert second == ["replied_fallback"]

    letters = await _rows(sessionmaker_for, DeadLetterJob)
    assert [letter.error for letter in letters] == ["openai_insufficient_quota"]
    haystack = "\n".join(
        [caplog.text, *first, *second, *(str(row_id) for row_id in enqueued)]
        + [f"{letter.payload}{letter.error}{letter.source_event_id}" for letter in letters]
    )
    for sentinel in (patient, reply, error_body, key):
        assert sentinel not in haystack, sentinel
