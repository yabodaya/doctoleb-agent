import asyncio
import logging
import time

import pytest

from app.api.health import PROBE_TIMEOUT_SECONDS, database_status, redis_status


async def test_ready_returns_200_when_both_dependencies_are_up(app, client):
    app.dependency_overrides[database_status] = lambda: "ok"
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_ready_returns_503_when_the_database_is_down(app, client):
    app.dependency_overrides[database_status] = lambda: "error"
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "error", "redis": "ok"}


async def test_ready_returns_503_when_redis_is_down(app, client):
    app.dependency_overrides[database_status] = lambda: "ok"
    app.dependency_overrides[redis_status] = lambda: "error"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "ok", "redis": "error"}


async def test_database_status_reports_error_when_the_probe_raises(monkeypatch):
    async def unreachable() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("app.api.health.ping_database", unreachable)

    assert await database_status() == "error"


async def test_redis_status_reports_error_when_the_probe_raises(monkeypatch):
    async def unreachable() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("app.api.health.ping_redis", unreachable)

    assert await redis_status() == "error"


@pytest.mark.parametrize("failure", [ValueError("odd"), RuntimeError("odder")])
async def test_ready_answers_503_for_unexpected_probe_failures(app, client, monkeypatch, failure):
    """Review Focus 4. A readiness endpoint that raises a 500 tells you nothing."""

    async def unreachable() -> None:
        raise failure

    monkeypatch.setattr("app.api.health.ping_database", unreachable)
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "error"


async def test_probe_failure_does_not_log_the_connection_string(monkeypatch, caplog):
    """Review Focus 2, hard rule 9. Connection errors carry the DSN, password included."""
    secret = "sup3rsecret"

    async def unreachable() -> None:
        raise OSError(f"could not connect to postgresql://doctoleb:{secret}@postgres:5432/db")

    monkeypatch.setattr("app.api.health.ping_database", unreachable)

    with caplog.at_level(logging.WARNING):
        assert await database_status() == "error"

    assert secret not in caplog.text
    assert "database" in caplog.text


async def test_ready_answers_503_when_a_probe_hangs(app, client, monkeypatch, caplog):
    """Review Focus 6.

    A dependency that accepts the connection but never answers must not hold the
    request open. The probe is bounded, so /health/ready answers in about
    PROBE_TIMEOUT_SECONDS rather than waiting out the ten-second sleep.
    """

    async def hangs() -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr("app.api.health.ping_database", hangs)
    app.dependency_overrides[redis_status] = lambda: "ok"

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        response = await client.get("/health/ready")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "error", "redis": "ok"}
    # Generous upper bound: the point is "bounded", not "exactly 2s on a loaded CI box".
    assert elapsed < PROBE_TIMEOUT_SECONDS + 3
    assert "timed out" in caplog.text
    assert "database" in caplog.text
