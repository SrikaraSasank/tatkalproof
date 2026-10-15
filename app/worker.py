"""Background jobs: expired-hold sweeper, waiting-room admitter, key cleanup.

Runs inside the API process for local dev (RUN_BACKGROUND_WORKERS=true), or as
its own container in docker-compose (`python -m app.worker`) so that running
4 API workers doesn't mean 4 sweepers.
"""

import asyncio
import logging
import time

from .config import get_settings
from .db import SessionLocal
from .flags import get_flags
from .idempotency import purge_old_keys
from .redis_client import get_redis
from .services.booking import release_expired_holds
from .waiting_room import admit_batch

log = logging.getLogger("tatkalproof.worker")


async def sweeper_loop(stop: asyncio.Event) -> None:
    s = get_settings()
    last_purge = 0.0
    while not stop.is_set():
        try:
            released = await release_expired_holds(SessionLocal, get_redis())
            if released:
                log.info("released %d expired holds", released)
            if time.monotonic() - last_purge > 3600:
                purged = await purge_old_keys(s.idempotency_key_ttl_hours)
                last_purge = time.monotonic()
                if purged:
                    log.info("purged %d old idempotency keys", purged)
        except Exception:
            log.exception("sweeper iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=s.sweeper_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def admitter_loop(stop: asyncio.Event) -> None:
    s = get_settings()
    while not stop.is_set():
        try:
            r = get_redis()
            flags = await get_flags(r)
            if flags.waiting_room_enabled:
                n = await admit_batch(r, flags.waiting_room_admit_per_second, s.waiting_room_pass_ttl_seconds)
                if n:
                    log.info("admitted %d users from waiting room", n)
        except Exception:
            log.exception("admitter iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def start_background_tasks() -> tuple[asyncio.Event, list[asyncio.Task]]:
    stop = asyncio.Event()
    tasks = [asyncio.create_task(sweeper_loop(stop)), asyncio.create_task(admitter_loop(stop))]
    return stop, tasks


async def stop_background_tasks(stop: asyncio.Event, tasks: list[asyncio.Task]) -> None:
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("worker started")
    stop, tasks = start_background_tasks()
    try:
        await asyncio.gather(*tasks)
    finally:
        await stop_background_tasks(stop, tasks)


if __name__ == "__main__":
    asyncio.run(_main())
