"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.whatsapp import router as whatsapp_router
from app.config import Settings, get_settings
from app.db.session import dispose_engine
from app.logging_config import configure_logging
from app.queue.arq_queue import close_job_queue
from app.queue.redis import close_redis

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield
    await dispose_engine()
    await close_redis()
    await close_job_queue()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. A factory, not a module-level app, so each test gets a
    clean instance and dependency overrides cannot leak between tests.

    `settings` is injectable for tests that need a differently configured app
    (a different APP_ENV, different Meta secrets). Production passes nothing and
    gets the process-wide settings.
    """
    settings = settings or get_settings()

    # VS-001 follow-up, closed here because VS-003 is what creates the exposure:
    # this API is about to sit behind a public tunnel so Meta can reach it. By
    # default it publishes no schema and no interactive docs.
    #
    # Gated on docs_enabled and NOT on app_env: the tunnel runs while
    # APP_ENV=development, so an app_env condition would be open exactly when the
    # API is reachable from the internet.
    #
    # openapi_url=None alone would disable /docs, but naming all three keeps the
    # intent readable and the test honest.
    app = FastAPI(
        title="Doctoleb WhatsApp Agent",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.include_router(health_router)
    app.include_router(whatsapp_router)

    if not settings.meta_app_secret:
        # Names only, never values (hard rule 9). Without this line, "every
        # handshake and every webhook is rejected" looks like a bug in the code
        # rather than a missing .env entry.
        logger.warning("META_APP_SECRET is not set: every WhatsApp webhook POST will be rejected")
    if not settings.meta_verify_token:
        logger.warning("META_VERIFY_TOKEN is not set: the Meta handshake will be rejected")
    if not settings.meta_access_token:
        # VS-004: receiving a message needs only the app secret, but REPLYING
        # needs a token. Without this line, every reply dead-letters with a
        # permanent 401 and the cause is three tables away.
        logger.warning("META_ACCESS_TOKEN is not set: every WhatsApp reply will fail")
    if not settings.whatsapp_tenant_map and not (
        settings.dev_tenant_id and settings.meta_phone_number_id
    ):
        # Hard rule 4: there is no default tenant, so an unmapped number is a
        # permanent failure per event. Names only, never values.
        logger.warning(
            "no tenant mapping configured: set WHATSAPP_TENANT_MAP, "
            "or DEV_TENANT_ID with META_PHONE_NUMBER_ID"
        )

    return app


# uvicorn target: `uvicorn app.main:app`
app = create_app()
