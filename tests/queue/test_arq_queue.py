"""The arq queue, against a stub pool. No Redis is needed to run these."""

import logging
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.queue import INBOX_JOB_NAME, ArqJobQueue, EnqueueError, JobQueue, inbox_job_id
from tests.queue.fakes import FakeJobQueue
from tests.whatsapp_factories import wamid

ROW_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")


def settings_with(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class StubPool:
    """Stands in for arq's ArqRedis. Records calls; returns what it is told to."""

    def __init__(self, result="job", error: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self._result = result
        self._error = error

    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._result


def queue_with(pool: StubPool, **setting_overrides) -> ArqJobQueue:
    queue = ArqJobQueue(settings_with(**setting_overrides))
    queue._pool = pool  # noqa: SLF001 - injecting the pool is the point
    return queue


async def test_the_job_id_is_the_row_id_under_an_inbox_prefix():
    """Requirement 1: enqueueing twice is harmless, because arq refuses the id.

    The prefix namespaces our keys inside a Redis that also holds arq's own.
    """
    pool = StubPool()

    await queue_with(pool).enqueue_inbox_event(ROW_ID)

    _, kwargs = pool.calls[0]
    assert kwargs["_job_id"] == f"inbox:{ROW_ID}"
    assert inbox_job_id(ROW_ID) == f"inbox:{ROW_ID}"


async def test_enqueueing_passes_the_row_id_and_nothing_else():
    """Hard rule 8: Redis holds one identifier of ours and no payload."""
    pool = StubPool()

    await queue_with(pool).enqueue_inbox_event(ROW_ID)

    args, _ = pool.calls[0]
    assert args == (INBOX_JOB_NAME, str(ROW_ID))


async def test_no_job_argument_or_job_id_contains_a_wamid():
    """Plan note C2, made executable.

    A wamid is base64 and decodes to include the patient's phone number, and a
    status event id carries the wamid of the message we sent TO the patient.
    Neither may reach Redis. The only way to be sure is that the queue never sees
    one: its signature takes a UUID.
    """
    pool = StubPool()

    await queue_with(pool).enqueue_inbox_event(ROW_ID)

    rendered = repr(pool.calls[0])
    assert wamid(1) not in rendered
    assert "msg:" not in rendered
    assert "status:" not in rendered


async def test_a_duplicate_job_id_is_not_an_error(caplog):
    """arq returns None when the id is already queued, running, or has a result.

    That is the dedup working, not a failure - and it is what lets the webhook
    re-enqueue a redelivery blindly.
    """
    pool = StubPool(result=None)

    with caplog.at_level(logging.DEBUG):
        await queue_with(pool).enqueue_inbox_event(ROW_ID)

    assert "already queued" in "\n".join(r.getMessage() for r in caplog.records)


async def test_a_redis_failure_becomes_an_enqueue_error(caplog):
    """So the endpoint never has to know what a redis exception looks like."""
    pool = StubPool(error=RedisConnectionError("connection refused"))

    with caplog.at_level(logging.DEBUG), pytest.raises(EnqueueError):
        await queue_with(pool).enqueue_inbox_event(ROW_ID)

    # Class name only: a redis error can carry the DSN, and the DSN carries a
    # password (hard rule 9).
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in rendered
    assert "connection refused" not in rendered


async def test_an_os_error_becomes_an_enqueue_error():
    pool = StubPool(error=OSError("no route to host"))

    with pytest.raises(EnqueueError):
        await queue_with(pool).enqueue_inbox_event(ROW_ID)


def test_the_pool_is_built_from_redis_url():
    """Hard rule 9: the DSN comes from the environment, never a literal."""
    from arq.connections import RedisSettings

    queue = ArqJobQueue(settings_with(redis_url="redis://elsewhere:6380/3"))
    expected = RedisSettings.from_dsn("redis://elsewhere:6380/3")

    assert queue._settings.redis_url == "redis://elsewhere:6380/3"  # noqa: SLF001
    assert expected.host == "elsewhere"


def test_both_queues_satisfy_the_protocol():
    """runtime_checkable, so the fake and the real one cannot drift apart."""
    assert isinstance(ArqJobQueue(settings_with()), JobQueue)
    assert isinstance(FakeJobQueue(), JobQueue)
