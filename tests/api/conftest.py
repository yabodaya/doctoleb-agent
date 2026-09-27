"""Fixtures for the webhook endpoints.

The database fixtures are re-exported from tests/db/conftest.py rather than
promoted to tests/conftest.py: promoting them would make every test in the suite
import Alembic and asyncpg for the sake of two modules.
"""

from typing import Any

import pytest

from app.config import Settings, get_settings
from app.db.session import get_session
from app.queue import get_job_queue
from tests.db.conftest import (  # noqa: F401  (re-exported fixtures)
    db_engine,
    db_session,
    migrated_database,
    second_session_factory,
    test_database_url,
)
from tests.queue.fakes import FakeJobQueue
from tests.whatsapp_factories import APP_SECRET, VERIFY_TOKEN


def meta_settings(**overrides: Any) -> Settings:
    """Settings with fake Meta credentials. Never the developer's real ones."""
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "meta_app_secret": APP_SECRET,
        "meta_verify_token": VERIFY_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def configure(app):
    """Point the app's settings dependency at fake Meta credentials.

    get_settings is used through Depends() in the handlers precisely so a test can
    replace it without touching the process environment or the lru_cache. Call
    the returned function again to change a value.
    """

    def apply(**overrides: Any) -> Settings:
        settings = meta_settings(**overrides)
        app.dependency_overrides[get_settings] = lambda: settings
        return settings

    apply()
    return apply


class ExplodingSession:
    """A session that fails the test if the endpoint touches the database.

    Used to prove the paths that must answer before any I/O: a rejected
    signature, and a payload with nothing to store.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the endpoint reached the database")

    async def commit(self) -> None:
        raise AssertionError("the endpoint committed")

    async def rollback(self) -> None:
        raise AssertionError("the endpoint rolled back")


@pytest.fixture
def no_database(app):
    """Override get_session with a session that must never be used."""

    async def override():
        yield ExplodingSession()

    app.dependency_overrides[get_session] = override


@pytest.fixture
def use_database(app, db_session):  # noqa: F811  (db_session is the re-exported fixture)
    """Run the endpoint against the rolled-back test-database session.

    The endpoint commits; db_session's join_transaction_mode="create_savepoint"
    makes that real for the session and still invisible to the next test.
    """

    async def override():
        yield db_session

    app.dependency_overrides[get_session] = override
    return db_session


@pytest.fixture(autouse=True)
def queue(app):
    """Every endpoint test gets a recording queue, not a real Redis connection.

    Autouse, because from VS-004 on the webhook enqueues on every successful
    POST. Without this every existing endpoint test would open a Redis pool, and
    VS-001's rule that `pytest` passes with nothing running would quietly die.

    Returns the fake, so a test can assert on what was enqueued.
    """
    fake = FakeJobQueue()
    app.dependency_overrides[get_job_queue] = lambda: fake
    return fake


@pytest.fixture
def failing_queue(app, queue):
    """Swap in a queue that raises, for the enqueue-failure path."""
    fake = FakeJobQueue(fail=True)
    app.dependency_overrides[get_job_queue] = lambda: fake
    return fake
