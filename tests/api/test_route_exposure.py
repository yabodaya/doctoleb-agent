"""Review Focus 9, and VS-001's follow-up.

VS-003 puts this API behind a public tunnel. Everything the tunnel can reach is
reachable by anyone who learns the URL, so what the app publishes is now a
security question rather than a convenience one.
"""

from fastapi import FastAPI

from app.config import Settings
from app.main import create_app


def _app(docs_enabled: bool = False, app_env: str = "development") -> FastAPI:
    """A fresh app with `docs_enabled` set, without touching the process env.

    app_env defaults to "development" on purpose: that is what runs while the
    tunnel is up, so every assertion below is made under the configuration the
    exposure actually happens in.

    create_app() reads settings once, at build time, so the override has to be in
    place before the app exists — which is why this builds its own rather than
    using the `app` fixture.
    """
    return create_app(
        settings=Settings(
            _env_file=None,
            app_env=app_env,
            docs_enabled=docs_enabled,
            database_url="postgresql+asyncpg://user:pw@db:5432/doctoleb",
            redis_url="redis://cache:6379/1",
        )
    )


async def test_docs_are_not_served_by_default_even_in_development(client_for):
    """Review Focus 9.

    APP_ENV is development here — the tunnel's own configuration. The tunnel
    would otherwise hand a stranger the full shape of every endpoint this repo
    will ever have, including the ones not written yet.
    """
    async with client_for(_app()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path


async def test_docs_are_served_only_when_docs_enabled_is_set(client_for):
    async with client_for(_app(docs_enabled=True)) as client:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200


async def test_health_still_answers_with_the_docs_off(client_for):
    # Disabling the docs must not disable the app. /health is how the container
    # healthcheck and the developer both check the process is alive.
    async with client_for(_app()) as client:
        assert (await client.get("/health")).status_code == 200


def _paths_and_methods(app: FastAPI) -> dict[str, set[str]]:
    """Every path this app actually serves, flattened.

    FastAPI 0.141 keeps an included router as a wrapper object on `app.routes`
    rather than flattening its APIRoutes onto it, so a plain comprehension over
    `app.routes` sees no paths at all and would pass by finding nothing. This
    walks whatever is there: real routes, plus any wrapper carrying a router of
    its own. Deliberately not `app.openapi()["paths"]`, which would miss a route
    marked include_in_schema=False - exactly the kind this test exists to catch.
    """
    found: dict[str, set[str]] = {}

    def walk(routes) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None:
                walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            if path:
                found.setdefault(path, set()).update(getattr(route, "methods", None) or set())

    walk(app.routes)
    return found


def test_the_app_exposes_only_the_expected_paths():
    """Review Focus 9.

    An inventory, not a spot check: the tunnel makes every route public, so a
    future slice adding an unauthenticated endpoint should fail here rather than
    be discovered from the outside.
    """
    served = _paths_and_methods(_app())

    assert set(served) == {"/health", "/health/ready", "/webhooks/whatsapp"}


def test_the_webhook_answers_both_methods_meta_uses():
    served = _paths_and_methods(_app())

    # GET is the one-time handshake, POST is every delivery. Nothing else.
    assert served["/webhooks/whatsapp"] == {"GET", "POST"}
