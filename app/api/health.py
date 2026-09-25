"""Health endpoints.

/health is liveness: is this process alive? It does no I/O on purpose, so a
database or Redis outage never makes an orchestrator kill a healthy API.
/health/ready is readiness: can this process actually serve traffic?
"""

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app.db.session import ping_database
from app.queue.redis import ping_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

DependencyStatus = Literal["ok", "error"]

# Hard rule 11 applied to readiness: every probe is bounded. A dependency that
# accepts the connection and then goes quiet is the case a plain `except` never
# catches — without this, the request hangs until the caller gives up and the
# endpoint reports nothing at all.
PROBE_TIMEOUT_SECONDS = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: DependencyStatus
    redis: DependencyStatus


async def database_status() -> DependencyStatus:
    """Probe Postgres. A FastAPI dependency, so tests can override it.

    Bounded and total: it always returns within PROBE_TIMEOUT_SECONDS and never
    raises. A probe exists to report a verdict, never to propagate a 500
    (Review Focus 4) and never to block the request (Review Focus 6).
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await ping_database()
    except TimeoutError:
        # Split from the general case so the log says which failure this was:
        # "refused" and "accepted then went silent" need different fixes.
        logger.warning("readiness probe timed out for dependency=database")
        return "error"
    except Exception:
        # Hard rule 9: the dependency name only. Connection errors embed the
        # DSN, password included, so the exception text must not be logged.
        logger.warning("readiness probe failed for dependency=database")
        return "error"
    return "ok"


async def redis_status() -> DependencyStatus:
    """Probe Redis. Same contract as database_status()."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await ping_redis()
    except TimeoutError:
        logger.warning("readiness probe timed out for dependency=redis")
        return "error"
    except Exception:
        logger.warning("readiness probe failed for dependency=redis")
        return "error"
    return "ok"


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness. Touches nothing external."""
    return HealthResponse(status="ok")


@router.get("/health/ready")
async def ready(
    response: Response,
    database: Annotated[DependencyStatus, Depends(database_status)],
    redis: Annotated[DependencyStatus, Depends(redis_status)],
) -> ReadinessResponse:
    """Readiness. 200 when every dependency answers, 503 when any does not.

    The body names the failing dependency either way, so the response alone is
    enough to debug from without reading logs.
    """
    healthy = database == "ok" and redis == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if healthy else "degraded",
        database=database,
        redis=redis,
    )
