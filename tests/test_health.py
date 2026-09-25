async def test_health_returns_ok(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_does_not_touch_dependencies(client, monkeypatch):
    """Review Focus 3. Liveness must stay 200 while Postgres and Redis are down."""

    async def must_not_be_called() -> None:
        raise AssertionError("liveness must not touch external dependencies")

    monkeypatch.setattr("app.api.health.ping_database", must_not_be_called)
    monkeypatch.setattr("app.api.health.ping_redis", must_not_be_called)

    response = await client.get("/health")

    assert response.status_code == 200
