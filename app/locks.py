"""Distributed seat locks on Redis.

A hold is `SET seat:{event}:{seat} <owner> NX EX <ttl>`: atomic, so exactly one
caller wins, and the TTL means a crashed client can never lock a seat forever.
Releasing uses a Lua compare-and-delete so a slow client whose lock already
expired can't delete somebody else's newer lock.
"""

RELEASE_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def seat_lock_key(event_id: int, seat_id: int) -> str:
    return f"seat:{event_id}:{seat_id}"


async def acquire_seat_lock(r, event_id: int, seat_id: int, owner: str, ttl_seconds: int) -> bool:
    return bool(await r.set(seat_lock_key(event_id, seat_id), owner, nx=True, ex=ttl_seconds))


async def release_seat_lock(r, event_id: int, seat_id: int, owner: str) -> bool:
    return bool(await r.eval(RELEASE_IF_OWNER, 1, seat_lock_key(event_id, seat_id), owner))


async def seat_lock_owner(r, event_id: int, seat_id: int) -> str | None:
    return await r.get(seat_lock_key(event_id, seat_id))
