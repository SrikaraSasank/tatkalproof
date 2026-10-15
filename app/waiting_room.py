"""Virtual waiting room (the "you are in a queue" page big ticket sites show).

Users join a per-event FIFO queue (a Redis sorted set scored by a global
sequence number). A background admitter pops N users per second with ZPOPMIN
(atomic, so nobody is admitted twice) and gives each a time-limited pass. The
booking endpoint only accepts users holding a pass, so the database sees a
steady trickle instead of a 10 AM tsunami.
"""

from .errors import NotAdmitted

ACTIVE_QUEUES = "queues:active"


def queue_key(event_id: int) -> str:
    return f"queue:{event_id}"


def pass_key(event_id: int, user_id: int) -> str:
    return f"pass:{event_id}:{user_id}"


async def join_queue(r, event_id: int, user_id: int) -> int | None:
    """Returns the 1-based queue position, or None if already admitted."""
    if await r.exists(pass_key(event_id, user_id)):
        return None
    seq = await r.incr("queue:seq")
    pipe = r.pipeline(transaction=True)
    pipe.zadd(queue_key(event_id), {str(user_id): seq}, nx=True)  # re-joining keeps your place
    pipe.sadd(ACTIVE_QUEUES, str(event_id))
    pipe.zrank(queue_key(event_id), str(user_id))
    _, _, rank = await pipe.execute()
    return int(rank) + 1


async def queue_status(r, event_id: int, user_id: int) -> tuple[bool, int | None, int | None]:
    """(admitted, position, pass_ttl_seconds)"""
    ttl = await r.ttl(pass_key(event_id, user_id))
    if ttl and ttl > 0:
        return True, None, int(ttl)
    rank = await r.zrank(queue_key(event_id), str(user_id))
    return False, (int(rank) + 1 if rank is not None else None), None


async def admit_batch(r, admit_per_event: int, pass_ttl: int) -> int:
    """Admit up to `admit_per_event` users from every active queue."""
    admitted = 0
    for event_id in await r.smembers(ACTIVE_QUEUES):
        popped = await r.zpopmin(queue_key(int(event_id)), admit_per_event)
        if not popped:
            await r.srem(ACTIVE_QUEUES, event_id)
            continue
        pipe = r.pipeline(transaction=False)
        for user_id, _score in popped:
            pipe.set(pass_key(int(event_id), int(user_id)), "1", ex=pass_ttl)
        await pipe.execute()
        admitted += len(popped)
    return admitted


async def ensure_admitted(r, event_id: int, user_id: int) -> None:
    if not await r.exists(pass_key(event_id, user_id)):
        raise NotAdmitted("Waiting room is active: join the queue at POST /queue/{event_id}/join and wait for your turn")
