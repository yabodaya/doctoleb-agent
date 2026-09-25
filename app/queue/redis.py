"""Redis client.

VS-001 needs this only so readiness can prove Redis is reachable. VS-004 adds
the enqueue interface in this package, which is why it lives under app/queue/
rather than somewhere generic.
"""

from redis.asyncio import Redis, from_url

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide Redis client, creating it on first use."""
    global _redis
    if _redis is None:
        _redis = from_url(get_settings().redis_url)
    return _redis


async def ping_redis() -> None:
    """Raise if Redis is not reachable. Returns None when it is."""
    await get_redis().ping()


async def close_redis() -> None:
    """Close the client. Called on app shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
