"""Small management CLI.

    python -m app.cli init-db     # create tables
    python -m app.cli seed        # create a demo event if none exist
"""

import asyncio
import sys
from datetime import timedelta

from sqlalchemy import func, select

from .db import SessionLocal, init_db
from .models import Event, utcnow
from .routers.admin import create_event_with_seats
from .schemas import EventCreate


async def seed() -> None:
    async with SessionLocal() as session:
        if await session.scalar(select(func.count()).select_from(Event)):
            print("events already exist, skipping seed")
            return
        event = await create_event_with_seats(
            session,
            EventCreate(
                name="12951 Rajdhani Express - Tatkal 3A",
                venue="Mumbai Central -> New Delhi",
                starts_at=utcnow() + timedelta(days=1),
                price=2450,
                rows=10,
                seats_per_row=10,
            ),
        )
        print(f"seeded event {event.id} with {event.total_seats} seats")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init-db":
        asyncio.run(init_db())
        print("tables created")
    elif cmd == "seed":
        asyncio.run(init_db())
        asyncio.run(seed())
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
