# ruff: noqa: E402
"""Shared test setup.

app.config.Settings has required fields, so the environment must be populated
before anything imports it. That is why these assignments sit above the imports.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://doctoleb:doctoleb@localhost:5432/doctoleb"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
def app():
    """A fresh app per test, so dependency_overrides never leak between tests."""
    return create_app()


@pytest.fixture
async def client(app):
    """An in-process HTTP client. ASGITransport calls the app directly, so no
    port is bound and the lifespan does not run — tests need neither."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
