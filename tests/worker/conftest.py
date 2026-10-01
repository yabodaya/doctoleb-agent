"""Fixtures for the worker jobs.

The database fixtures are re-exported from tests/db/conftest.py rather than
promoted to tests/conftest.py, for the same reason tests/api/conftest.py does it:
promoting them would make every test in the suite import Alembic and asyncpg.

`second_session_factory` comes along too - the concurrency tests need two
genuinely independent connections, which the rollback-wrapped db_session cannot
provide.
"""

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from arq.worker import Retry
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels.whatsapp.client import MetaClient
from app.config import Settings, get_settings
from app.db.models import WebhookInbox
from app.db.session import SESSION_OPTIONS, get_session
from app.integrations.booking.fake import FakeBookingClient
from app.main import create_app
from app.queue import get_job_queue
from app.tenants.resolver import ConfigTenantResolver
from app.worker.jobs.inbox import process_inbox_event
from tests.db import factories as f
from tests.db.conftest import (  # noqa: F401  (re-exported fixtures)
    db_engine,
    db_session,
    migrated_database,
    second_session_factory,
    test_database_url,
)
from tests.integrations.fakes import FakeChatClient
from tests.queue.fakes import FakeJobQueue
from tests.whatsapp_factories import (
    APP_SECRET,
    PHONE_NUMBER_ID,
    VERIFY_TOKEN,
    contact,
    phone,
    signed,
    text_message,
    wamid,
)

ACCESS_TOKEN = "test-access-token-not-a-real-one"


def worker_settings(**overrides: Any) -> Settings:
    """Settings for a job under test. Never the developer's real ones."""
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "meta_access_token": ACCESS_TOKEN,
        "meta_api_version": "v21.0",
        "meta_api_base_url": "https://graph.facebook.com",
        "whatsapp_tenant_map": f'{{"{PHONE_NUMBER_ID}": "{f.TENANT_A}"}}',
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Meta:
    """A Meta transport that records, scripts and can stall.

    Used everywhere instead of a real client: nothing in this suite may reach the
    network, and several tests need to know exactly how many sends happened.
    """

    def __init__(self, *responses: httpx.Response, hook=None):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses) or [ok_response()]
        self._hook = hook

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._hook is not None:
            await self._hook(request)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    @property
    def sends(self) -> int:
        return len(self.requests)


def ok_response(n: int = 9) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": phone(n), "wa_id": phone(n)}],
            "messages": [{"id": wamid(n), "message_status": "accepted"}],
        },
    )


def meta_client(transport, settings: Settings | None = None) -> MetaClient:
    settings = settings or worker_settings()
    return MetaClient(httpx.AsyncClient(transport=httpx.MockTransport(transport)), settings)


def message_payload(n: int = 1, **message_overrides: Any) -> dict[str, Any]:
    """A webhook_inbox payload for one inbound message, in VS-003's stored shape."""
    return {
        "kind": "message",
        "object": "whatsapp_business_account",
        "entry_id": "200000000000002",
        "field": "messages",
        "metadata": {"display_phone_number": phone(999), "phone_number_id": PHONE_NUMBER_ID},
        "contacts": [contact(n)],
        "item": text_message(n, **message_overrides),
    }


def status_payload(n: int = 1, state: str = "sent", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": wamid(n),
        "status": state,
        "timestamp": "1730000001",
        "recipient_id": phone(n),
    }
    item.update(overrides)
    return {
        "kind": "status",
        "object": "whatsapp_business_account",
        "entry_id": "200000000000002",
        "field": "messages",
        "metadata": {"display_phone_number": phone(999), "phone_number_id": PHONE_NUMBER_ID},
        "item": item,
    }


@pytest.fixture
def sessionmaker_for(db_engine):  # noqa: F811
    """A session factory whose sessions really commit.

    The job commits three times and reads its own writes back across
    transactions, so it cannot run on the rollback-wrapped db_session. Tests
    using this clean up after themselves through the `clean_database` fixture.

    Built with **SESSION_OPTIONS - the same options app/db/session.py gives the
    production factory - and not a hand-written expire_on_commit=False. The job
    reads row.payload after committing the claim, which works only because the
    session does not expire on commit; a harness that set that itself would keep
    passing if production ever stopped doing it, and the job would fail live with
    MissingGreenlet. tests/db/test_session.py asserts the two match.
    """
    return async_sessionmaker(bind=db_engine, **SESSION_OPTIONS)


@pytest.fixture(autouse=True)
async def clean_database(request, db_engine):  # noqa: F811
    """Truncate between tests, because these tests commit for real.

    Only for tests that asked for a database; everything else skips it.
    """
    if "sessionmaker_for" not in request.fixturenames:
        yield
        return
    yield
    import sqlalchemy as sa

    async with db_engine.begin() as connection:
        await connection.execute(
            sa.text(
                # tool_executions before agent_runs is not strictly needed -
                # one TRUNCATE handles the FK - but the order documents it.
                "TRUNCATE booking_actions, tool_executions, agent_runs, webhook_inbox, "
                "dead_letter_jobs, messages, conversations, contact_identities, contacts"
            )
        )


async def store_event(sessionmaker, payload: dict[str, Any], n: int = 1, **overrides) -> uuid.UUID:
    """Put one row in webhook_inbox the way the webhook would, and commit."""
    values: dict[str, Any] = {
        "provider": "whatsapp",
        "provider_event_id": f"evt-{n:08d}",
        "payload": payload,
        "status": "RECEIVED",
        "attempts": 0,
    }
    values.update(overrides)
    async with sessionmaker() as session:
        row = WebhookInbox(**values)
        session.add(row)
        await session.commit()
        return row.id


# Tuesday 29 September 2026, 10:00 clinic local. The same instant Task 9's
# acceptance test freezes, so "tomorrow" is Wednesday the 30th and Dr. Karim's
# afternoon is 14:00, 14:20, 15:40, 16:20.
FROZEN = dt.datetime(2026, 9, 29, 7, tzinfo=dt.UTC)


def FROZEN_CLOCK() -> dt.datetime:  # noqa: N802 - it is a clock, not a class
    return FROZEN


def job_context(sessionmaker, meta: MetaClient, settings: Settings | None = None, **overrides):
    """The arq ctx dict a job is called with."""
    settings = settings or worker_settings()
    ctx: dict[str, Any] = {
        "settings": settings,
        "sessionmaker": sessionmaker,
        "meta": meta,
        # A fake model by default, so every VS-004 test runs unchanged and no
        # test can reach OpenAI. A test that cares passes chat=FakeChatClient(...)
        # or the real OpenAIChatClient over an httpx2.MockTransport.
        "chat": FakeChatClient(),
        # VS-006: a FROZEN clock and the demo booking data by default, so every
        # VS-004 and VS-005 test runs unchanged (FakeChatClient(ok()) asks for
        # no tools) while a test that cares can pass a RecordingBooking.
        "clock": FROZEN_CLOCK,
        "booking": FakeBookingClient.demo(clock=FROZEN_CLOCK),
        # VS-007: None by default, so every VS-004 to VS-006 test runs with no
        # booking wiring at all and is unaffected. A booking test passes the SAME
        # InMemoryBookingService for both roles, as the worker does.
        "patient_bookings": None,
        "resolver": ConfigTenantResolver.from_settings(settings),
        "job_try": 1,
    }
    ctx.update(overrides)
    return ctx


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
        "whatsapp_tenant_map": f'{{"100000000000001": "{f.TENANT_A}"}}',
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


def booking_service(clock=FROZEN_CLOCK, **options):
    """One `InMemoryBookingService` for a test, for BOTH booking roles.

    Per test, never shared: the service holds an `asyncio.Lock`, and a lock binds to
    the loop it is first contended in (plan check U5). `counter_ids()` makes the ids
    readable, so an assertion can name `hold_1` and `apt_1`.
    """
    from app.integrations.booking.fake import FakeBookingClient as _Fake
    from app.integrations.booking.memory import InMemoryBookingService
    from tests.integrations.booking_fakes import counter_ids

    return InMemoryBookingService(
        _Fake.demo(clock=clock),
        clock,
        id_secret=b"a fixed secret for the worker booking tests",
        new_id=counter_ids(),
        **options,
    )
