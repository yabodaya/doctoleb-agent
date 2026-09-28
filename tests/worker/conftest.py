"""Fixtures for the worker jobs.

The database fixtures are re-exported from tests/db/conftest.py rather than
promoted to tests/conftest.py, for the same reason tests/api/conftest.py does it:
promoting them would make every test in the suite import Alembic and asyncpg.

`second_session_factory` comes along too - the concurrency tests need two
genuinely independent connections, which the rollback-wrapped db_session cannot
provide.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels.whatsapp.client import MetaClient
from app.config import Settings
from app.db.models import WebhookInbox
from app.db.session import SESSION_OPTIONS
from app.tenants import ConfigTenantResolver
from tests.db import factories as f
from tests.db.conftest import (  # noqa: F401  (re-exported fixtures)
    db_engine,
    db_session,
    migrated_database,
    second_session_factory,
    test_database_url,
)
from tests.whatsapp_factories import PHONE_NUMBER_ID, contact, phone, text_message, wamid

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
                "TRUNCATE webhook_inbox, dead_letter_jobs, messages, "
                "conversations, contact_identities, contacts"
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


def job_context(sessionmaker, meta: MetaClient, settings: Settings | None = None, **overrides):
    """The arq ctx dict a job is called with."""
    settings = settings or worker_settings()
    ctx: dict[str, Any] = {
        "settings": settings,
        "sessionmaker": sessionmaker,
        "meta": meta,
        "resolver": ConfigTenantResolver.from_settings(settings),
        "job_try": 1,
    }
    ctx.update(overrides)
    return ctx
