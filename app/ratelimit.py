"""Sliding-window-counter rate limiter on Redis.

A fixed one-second window lets a client send 2x the limit across a window
boundary (burst at x.999s and again at y.000s). The sliding-window counter
weights the previous window by how much of it still overlaps "the last second",
which smooths that out with only two Redis keys per client.
"""

import math
import time

from fastapi import Depends

from .auth import CurrentUser, get_current_user
from .errors import RateLimited
from .flags import get_flags
from .metrics import bump
from .redis_client import get_redis

WINDOW_SECONDS = 1.0


async def check_rate_limit(r, identity: str, limit: int, now: float | None = None) -> tuple[bool, float]:
    now = time.time() if now is None else now
    window = int(now // WINDOW_SECONDS)
    elapsed_fraction = (now % WINDOW_SECONDS) / WINDOW_SECONDS
    cur_key = f"rl:{identity}:{window}"
    prev_key = f"rl:{identity}:{window - 1}"

    pipe = r.pipeline(transaction=False)
    pipe.incr(cur_key)
    pipe.expire(cur_key, int(WINDOW_SECONDS * 2) + 1)
    pipe.get(prev_key)
    current, _, previous = await pipe.execute()

    estimated = int(previous or 0) * (1 - elapsed_fraction) + int(current)
    if estimated <= limit:
        return True, 0.0
    retry_after = max(WINDOW_SECONDS * (1 - elapsed_fraction), 0.05)
    return False, retry_after


async def booking_rate_limit(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency: limits booking calls per user."""
    r = get_redis()
    flags = await get_flags(r)
    if flags.rate_limit_enabled:
        allowed, retry_after = await check_rate_limit(r, f"user:{user.id}", flags.rate_limit_per_second)
        if not allowed:
            await bump(r, "rate_limited")
            raise RateLimited(
                "Too many booking requests, slow down",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
    return user
