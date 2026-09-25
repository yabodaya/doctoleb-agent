"""Async SQLAlchemy engine.

VS-001 needs this only so readiness can prove Postgres is reachable.
VS-002 adds models, a session factory and repositories on the same engine.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None


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
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


async def ping_database() -> None:
    """Raise if Postgres is not reachable. Returns None when it is."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    """Close the connection pool. Called on app shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
