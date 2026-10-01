# ruff: noqa: E402
"""Shared test setup.

app.config.Settings has required fields, so the environment must be populated
before anything imports it. That is why these assignments sit above the imports.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# 127.0.0.1, not localhost. On Windows "localhost" resolves to ::1 first, and
# nothing is listening there - compose publishes on 127.0.0.1 - so every new
# connection pays a failed IPv6 attempt before falling back to IPv4. With one
# engine per database test that is the difference between a ~2 minute host run
# and a ~15 second one. Inside the container DATABASE_URL is already set from
# .env, so this default never applies there.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://doctoleb:doctoleb@127.0.0.1:5432/doctoleb"
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

import httpx
import httpx2
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


@pytest.fixture
def client_for():
    """An async-context client factory for a caller-built app.

    The `client` fixture covers the common case (the `app` fixture's app). Tests
    that need an app configured differently — a different APP_ENV, different Meta
    secrets — build it themselves and wrap it with this.
    """

    def build(app):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return build


@pytest.fixture(autouse=True)
def no_real_http2_transport(monkeypatch):
    """No test may reach OpenAI (VS-005 requirement 1).

    The OpenAI SDK sends through httpx2's real transport - a different package
    from the httpx the Meta client uses (plan conflict C8) - unless a test hands
    it an httpx2.MockTransport. Making the real one raise turns "a test forgot
    the fake" into a loud failure instead of a request billed to whichever key
    happens to be in the developer's environment.

    Verified against openai 3.20.0: the SDK propagates a transport's own
    exception unchanged rather than wrapping it in APIConnectionError, so this
    cannot be mistaken for an outage and retried five times.
    """

    async def refuse(self, request):
        raise RuntimeError("a test tried to reach the network through httpx2")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def no_real_http_transport(monkeypatch):
    """No test may reach Meta either (VS-008, plan conflict C10).

    The sibling fixture above blocks httpx2, which is what the OpenAI SDK uses.
    The Meta sender and VS-008's media client use httpx - a different package -
    so until this fixture existed a test that forgot a MockTransport would have
    tried to reach graph.facebook.com with whatever token was in the
    developer's environment. That matters more from this slice on: the media
    client sends the access token to a host it read out of a response body, so
    "no test reaches the real transport" is part of how the token stays ours.

    MockTransport and ASGITransport are different classes, so replacing this
    one method leaves every existing test untouched - the whole suite was run
    with this in place before it was added (plan P5: 1008 passed).
    """

    async def refuse(self, request):
        raise RuntimeError("a test tried to reach the network through httpx")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
