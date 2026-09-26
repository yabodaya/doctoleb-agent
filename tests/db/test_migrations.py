"""Acceptance: `alembic upgrade head` on an empty DB works, and downgrade works."""

import asyncio
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from tests.db.conftest import _ensure_database, alembic_config

pytestmark = pytest.mark.db

LIFECYCLE_DATABASE_NAME = "doctoleb_test_migrations"

EXPECTED_TABLES = {
    "webhook_inbox",
    "contacts",
    "contact_identities",
    "conversations",
    "messages",
    "dead_letter_jobs",
    "alembic_version",
}


@pytest.fixture
def lifecycle_url(test_database_url: str) -> str:
    """A throwaway database, so upgrading and downgrading here cannot disturb
    the shared doctoleb_test that the other database tests rely on."""
    parsed = urlparse(test_database_url)
    url = urlunparse(parsed._replace(path=f"/{LIFECYCLE_DATABASE_NAME}"))
    asyncio.run(_ensure_database(url))
    command.downgrade(alembic_config(url), "base")
    return url


def _table_names(url: str) -> set[str]:
    async def _read() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
                )
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def test_upgrade_head_on_an_empty_database_creates_every_table(lifecycle_url: str):
    assert _table_names(lifecycle_url) <= {"alembic_version"}
    command.upgrade(alembic_config(lifecycle_url), "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_downgrade_base_leaves_nothing_behind(lifecycle_url: str):
    # Review Focus 4. "downgrade works" usually means "did not raise". A leftover
    # index or constraint makes the NEXT upgrade fail on a database that looks
    # empty.
    command.upgrade(alembic_config(lifecycle_url), "head")
    command.downgrade(alembic_config(lifecycle_url), "base")
    assert _table_names(lifecycle_url) == {"alembic_version"}


def test_upgrade_downgrade_upgrade_is_repeatable(lifecycle_url: str):
    config = alembic_config(lifecycle_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_models_and_migrations_do_not_drift(migrated_database: str):
    # Review Focus 5. A column added to a model and never migrated works on every
    # machine whose database someone fixed by hand, and fails on a fresh deploy.
    #
    # Covers: added/removed tables and columns, type changes, nullability,
    # indexes, unique constraints. Does NOT cover CHECK constraints — alembic
    # has no comparison for them at all. tests/db/test_constraints.py proves
    # those at runtime instead.
    async def _diff() -> list:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_compare)
        finally:
            await engine.dispose()

    def _compare(sync_connection) -> list:
        context = MigrationContext.configure(
            sync_connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return compare_metadata(context, Base.metadata)

    assert asyncio.run(_diff()) == []
