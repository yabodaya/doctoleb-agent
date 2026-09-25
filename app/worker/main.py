"""arq worker entrypoint: `arq app.worker.main.WorkerSettings`.

Run as its own process, separate from the api, so slow work never happens
inside a webhook request (hard rule 1). VS-004 registers the real jobs here.

VS-001 asks for an "empty" worker, but arq refuses to start with no functions
and no cron jobs registered, so a single trivial `ping` job stands in. It also
gives us a way to prove the worker really consumes from Redis.
"""

import logging
from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    """A no-op job, used to verify the worker is consuming from Redis."""
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("worker stopped")


class WorkerSettings:
    """arq reads these as plain class attributes."""

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    functions = [ping]
    on_startup = startup
    on_shutdown = shutdown
