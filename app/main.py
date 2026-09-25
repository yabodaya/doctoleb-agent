"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield


def create_app() -> FastAPI:
    """Build the app. A factory, not a module-level app, so each test gets a
    clean instance and dependency overrides cannot leak between tests."""
    app = FastAPI(title="Doctoleb WhatsApp Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app


# uvicorn target: `uvicorn app.main:app`
app = create_app()
