"""arq worker entrypoint: `arq app.worker.main.WorkerSettings`.

Run as its own process, separate from the api, so slow work never happens inside
a webhook request (hard rule 1). This is where the first genuinely slow thing in
the repo lives — the Meta call — and it lives on the far side of the queue.
"""

import logging
from typing import Any

import httpx
from arq.connections import RedisSettings
from arq.worker import func

from app.channels.whatsapp.client import MetaClient
from app.config import get_settings
from app.db.session import dispose_engine, get_sessionmaker
from app.logging_config import configure_logging
from app.tenants import ConfigTenantResolver
from app.worker.jobs import process_inbox_event

logger = logging.getLogger(__name__)

_settings = get_settings()


async def ping(ctx: dict[str, Any]) -> str:
    """A no-op job, used to verify the worker is consuming from Redis.

    Kept from VS-001: it is still the cheapest proof that the worker is alive,
    and it costs one function.
    """
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    """Build everything the jobs share, once per process.

    One httpx client for the whole worker (plan assumption A8): connection reuse,
    and a client per job leaks sockets under load.

    The tenant resolver is built here rather than per job, so a broken
    WHATSAPP_TENANT_MAP is one startup failure per worker instead of a parse per
    event - while an UNMAPPED number is still a per-event dead letter, because
    that is resolve()'s job, not the constructor's.
    """
    configure_logging()
    settings = get_settings()
    http = httpx.AsyncClient()
    ctx["settings"] = settings
    ctx["sessionmaker"] = get_sessionmaker()
    ctx["http"] = http
    ctx["meta"] = MetaClient(http, settings)
    ctx["resolver"] = ConfigTenantResolver.from_settings(settings)
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    http: httpx.AsyncClient | None = ctx.get("http")
    if http is not None:
        await http.aclose()
    await dispose_engine()
    logger.info("worker stopped")


class WorkerSettings:
    """arq reads these as plain class attributes."""

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [
        ping,
        # max_tries and the timeout come from settings (hard rule 11). The
        # timeout must stay below the claim lease, or the lease expires while the
        # job is still inside the Meta call and a second worker sends the same
        # reply - Settings.claim_lease_seconds derives itself to guarantee that.
        func(
            process_inbox_event,
            name="process_inbox_event",
            max_tries=_settings.job_max_tries,
            timeout=_settings.job_timeout_seconds,
        ),
    ]
    # The same ceiling for anything registered later without its own.
    max_tries = _settings.job_max_tries
    on_startup = startup
    on_shutdown = shutdown
