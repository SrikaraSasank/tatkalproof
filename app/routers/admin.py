from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_admin
from ..db import SessionLocal, get_session
from ..errors import NotFound
from ..flags import get_flags, reset_flags, update_flags
from ..metrics import reset_metrics
from ..models import Booking, Event, Seat, SeatStatus
from ..redis_client import get_redis
from ..schemas import EventCreate, EventOut, FlagsIn
from ..services.booking import release_expired_holds
from ..waiting_room import ACTIVE_QUEUES, queue_key

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def seat_labels(rows: int, per_row: int) -> list[str]:
    return [f"{chr(ord('A') + r)}{n}" for r in range(rows) for n in range(1, per_row + 1)]


async def create_event_with_seats(session: AsyncSession, body: EventCreate) -> Event:
    event = Event(
        name=body.name, venue=body.venue, starts_at=body.starts_at, price=body.price,
        total_seats=body.rows * body.seats_per_row, seats_per_row=body.seats_per_row,
    )
    session.add(event)
    await session.flush()
    session.add_all(Seat(event_id=event.id, label=label) for label in seat_labels(body.rows, body.seats_per_row))
    await session.commit()
    return event


@router.post("/events", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(body: EventCreate, session: AsyncSession = Depends(get_session)) -> Event:
    return await create_event_with_seats(session, body)


@router.post("/events/{event_id}/reset")
async def reset_event(event_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Wipe all bookings and free every seat, so you can run another rush."""
    if await session.get(Event, event_id) is None:
        raise NotFound(f"Event {event_id} not found")
    deleted = await session.execute(delete(Booking).where(Booking.event_id == event_id))
    await session.execute(
        update(Seat)
        .where(Seat.event_id == event_id)
        .values(status=SeatStatus.AVAILABLE, held_by_user_id=None, hold_expires_at=None, version=0)
    )
    await session.commit()

    r = get_redis()
    keys = [k async for k in r.scan_iter(match=f"seat:{event_id}:*", count=1000)]
    keys += [k async for k in r.scan_iter(match=f"pass:{event_id}:*", count=1000)]
    keys.append(queue_key(event_id))
    await r.delete(*keys)
    await r.srem(ACTIVE_QUEUES, str(event_id))
    await reset_metrics(r)
    return {"event_id": event_id, "bookings_deleted": deleted.rowcount, "redis_keys_cleared": len(keys)}


@router.post("/metrics/reset")
async def metrics_reset() -> dict:
    await reset_metrics(get_redis())
    return {"ok": True}


@router.get("/flags")
async def read_flags() -> dict:
    return (await get_flags(get_redis())).__dict__


@router.put("/flags")
async def write_flags(body: FlagsIn) -> dict:
    return (await update_flags(get_redis(), body.model_dump())).__dict__


@router.delete("/flags")
async def clear_flags() -> dict:
    return (await reset_flags(get_redis())).__dict__


@router.post("/sweep")
async def sweep_now() -> dict:
    """Run the expired-hold sweeper immediately (normally runs every few seconds)."""
    return {"released": await release_expired_holds(SessionLocal, get_redis())}
