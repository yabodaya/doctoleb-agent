"""Fixtures for tests that need a real PostgreSQL.

Every fixture here is allowed to skip. VS-001's rule that `pytest` passes with
nothing running still holds: with no database, these tests report SKIPPED with a
reason and the rest of the suite is untouched.

The two session-scoped fixtures are SYNCHRONOUS on purpose. Alembic's async
template calls asyncio.run() inside env.py; calling it from inside a
pytest-asyncio coroutine raises
    RuntimeError: asyncio.run() cannot be called from a running event loop.
A sync fixture runs outside the loop and the problem disappears.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_NAME = "doctoleb_test"


def _derive_test_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    parsed = urlparse(os.environ["DATABASE_URL"])
    return urlunparse(parsed._replace(path=f"/{TEST_DATABASE_NAME}"))


def alembic_config(url: str) -> Config:
    """An Alembic Config pointed at `url`."""
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _ensure_database(url: str) -> None:
    """Create the target database if it is missing.

    Connects with raw asyncpg to the `postgres` maintenance database: CREATE
    DATABASE cannot run inside a transaction, and SQLAlchemy wraps everything
    in one by default.
    """
    parsed = urlparse(url)
    target = parsed.path.lstrip("/")
    admin_dsn = urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))
    connection = await asyncpg.connect(admin_dsn)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target)
        if not exists:
            await connection.execute(f'CREATE DATABASE "{target}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The test DSN, or skip every database test with a readable reason."""
    url = _derive_test_url()
    parsed = urlparse(url)
    try:
        asyncio.run(_ensure_database(url))
    except (OSError, asyncpg.PostgresError) as error:
        # Host and port only. The DSN carries a password (hard rule 9).
        pytest.skip(
            f"no PostgreSQL at {parsed.hostname}:{parsed.port} "
            f"({type(error).__name__}); run `docker compose up -d postgres`"
        )
    return url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Bring the test database to head, once per test session."""
    command.upgrade(alembic_config(test_database_url), "head")
    return test_database_url


@pytest.fixture
async def db_engine(migrated_database: str) -> AsyncIterator:
    """Function-scoped on purpose.

    pytest-asyncio runs this project with asyncio_default_fixture_loop_scope =
    "function"; a session-scoped async fixture would bind an engine to a loop
    that closes after the first test. Creating an engine is cheap.
    """
    engine = create_async_engine(migrated_database)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    join_transaction_mode="create_savepoint" lets a test call session.commit()
    to exercise real commit behaviour without ending the outer transaction, so
    the next test still starts from an empty database.

    Everything in one test therefore shares one transaction, which means
    PostgreSQL's now() is a single constant throughout. Tests that assert a
    timestamp moved must first set it to a fixed past value.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
def second_session_factory(db_engine):
    """A factory for genuinely independent sessions.

    Concurrency tests need two connections that cannot see each other's
    uncommitted rows, which the rollback-wrapped db_session cannot provide.
    Tests using this clean up after themselves.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)
