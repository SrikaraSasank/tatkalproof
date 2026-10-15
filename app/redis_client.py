from redis import asyncio as aioredis

from .config import get_settings

_redis = None


def get_redis():
    """Process-wide Redis client. REDIS_URL=fake:// gives an in-memory fake
    (fakeredis, with Lua support) so tests and demos need no Redis server."""
    global _redis
    if _redis is None:
        url = get_settings().redis_url
        if url.startswith("fake://"):
            from fakeredis import FakeAsyncRedis

            _redis = FakeAsyncRedis(decode_responses=True)
        else:
            _redis = aioredis.from_url(url, decode_responses=True, max_connections=200)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
