"""The arq implementation of JobQueue."""

import logging
import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.queue.interface import EnqueueError, JobQueue

logger = logging.getLogger(__name__)

# The worker registers the function under this name. A constant rather than a
# string literal in two files, because a typo would enqueue jobs that no worker
# will ever claim, silently.
INBOX_JOB_NAME = "process_inbox_event"


def inbox_job_id(row_id: uuid.UUID) -> str:
    """The arq job id for one inbox event.

    Prefixed rather than a bare uuid: Redis holds arq's own keys alongside ours,
    and a key seen in redis-cli should say what it is.

    Stable across redeliveries for free - the row id does not change when Meta
    redelivers, because store_if_new did not create a new row - which is exactly
    what lets arq suppress the repeat.
    """
    return f"inbox:{row_id}"


class ArqJobQueue(JobQueue):
    """Hands jobs to arq, and translates redis failures into one error type.

    The pool is created lazily and kept: arq needs its own connection pool, which
    is not the redis.asyncio client in app/queue/redis.py that readiness uses.
    Both read REDIS_URL (hard rule 9).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._pool: ArqRedis | None = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(RedisSettings.from_dsn(self._settings.redis_url))
        return self._pool

    async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None:
        """Enqueue one job, keyed by the row id so a repeat is harmless.

        The argument is str(row_id) rather than the UUID object: arq pickles job
        arguments, and a plain string is the least surprising thing to find in
        Redis and the easiest to keep working if the serialiser is ever changed.
        """
        try:
            pool = await self._get_pool()
            job = await pool.enqueue_job(INBOX_JOB_NAME, str(row_id), _job_id=inbox_job_id(row_id))
        except (RedisError, OSError) as error:
            # Class name only. A redis error can carry the DSN, and the DSN can
            # carry a password (hard rule 9).
            logger.error("enqueue failed event_id=%s error=%s", row_id, type(error).__name__)
            raise EnqueueError(type(error).__name__) from None

        if job is None:
            # arq returns None when a job with this id is queued, running, or has
            # a kept result: the same event is already on its way. A success, and
            # the reason the webhook can re-enqueue a redelivery blindly.
            #
            # It is a SHORT-LIVED key - results expire - which is why the worker
            # also checks webhook_inbox.status. See "Idempotency: the four keys".
            logger.debug("inbox job already queued event_id=%s", row_id)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: ArqJobQueue | None = None


def get_job_queue() -> JobQueue:
    """FastAPI dependency: the process-wide queue, built on first use.

    A dependency rather than a module-level import in the handler, so a test can
    override it with a fake without touching Redis or monkeypatching a module.
    """
    global _queue
    if _queue is None:
        _queue = ArqJobQueue()
    return _queue


async def close_job_queue() -> None:
    """Close the pool. Called on app shutdown, next to close_redis()."""
    global _queue
    if _queue is not None:
        await _queue.aclose()
        _queue = None
