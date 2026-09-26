"""Alembic environment.

Three things differ from the generated template:
  * the DSN comes from app.config, never from alembic.ini (hard rule 9);
  * app.db.models is imported for its side effects, so every table is on
    Base.metadata before autogenerate compares anything. A model nobody
    imported is a table Alembic proposes to DROP;
  * fileConfig is called with disable_existing_loggers=False.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.db.models  # noqa: F401  (import for side effects: registers the tables)
from app.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silences every logger
    # configured before this call. The test suite runs migrations in-process
    # (tests/db sorts before tests/test_readiness.py), so the default would
    # disable app.api.health's logger and make VS-001's caplog assertions fail
    # in the full run only — green file-by-file, red together.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """A caller-supplied URL wins; otherwise the app's configured DSN."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these, a changed column type or server default is invisible to
        # autogenerate. Note that no option makes it compare CHECK constraints;
        # see Review Focus 5.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.")
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
