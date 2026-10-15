import pytest

from app.ratelimit import check_rate_limit
from app.redis_client import get_redis

pytestmark = pytest.mark.asyncio


async def test_sliding_window_allows_up_to_limit():
    r = get_redis()
    results = [await check_rate_limit(r, "unit", 5, now=1000.5) for _ in range(7)]
    assert [ok for ok, _ in results] == [True] * 5 + [False] * 2


async def test_sliding_window_counts_previous_window():
    r = get_redis()
    for _ in range(5):
        await check_rate_limit(r, "edge", 5, now=2000.9)  # fill window 2000
    # 0.1s into the next window, 90% of the previous window still counts:
    # 5 * 0.9 + 1 = 5.5 > 5, so the boundary burst is blocked.
    ok, retry = await check_rate_limit(r, "edge", 5, now=2001.1)
    assert ok is False and retry > 0
