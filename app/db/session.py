"""Async SQLAlchemy engine.

VS-001 needs this only so readiness can prove Postgres is reachable.
VS-002 adds models, a session factory and repositories on the same engine.
"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use.

    Built lazily rather than at import time, so importing app.db.session never
    requires a configured environment.
    """
    global _engine
    if _engine is None:
        # pool_pre_ping discards connections the database closed while idle,
        # which is what makes readiness report the truth rather than a stale
        # pooled socket.
        #
        # hide_parameters=True is hard rule 8 applied to every statement at once.
        # Without it SQLAlchemy appends the bound parameters to every DBAPI error
        # message as `[parameters: ...]`, and in this repo those are message text,
        # raw Meta payloads and phone numbers. One logger.exception() on any query
        # would then leak patient content, including from code no slice has
        # written yet.
        _engine = create_async_engine(
            get_settings().database_url, pool_pre_ping=True, hide_parameters=True
        )
    return _engine


async def ping_database() -> None:
    """Raise if Postgres is not reachable. Returns None when it is."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, building it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        # expire_on_commit=False: after a commit, the caller can still read the
        # attributes of the object it just saved without a second round trip.
        # With the default, every attribute access after commit re-queries, and
        # in async code that raises MissingGreenlet instead of being merely slow.
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """Close the connection pool. Called on app shutdown."""
    global _engine, _sessionmaker
    _sessionmaker = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None
