"""Fixtures for the webhook endpoints.

The database fixtures are re-exported from tests/db/conftest.py rather than
promoted to tests/conftest.py: promoting them would make every test in the suite
import Alembic and asyncpg for the sake of two modules.
"""

from typing import Any

import pytest

from app.config import Settings, get_settings
from app.db.session import get_session
from tests.db.conftest import (  # noqa: F401  (re-exported fixtures)
    db_engine,
    db_session,
    migrated_database,
    test_database_url,
)
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
