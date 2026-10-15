from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..errors import NotFound
from ..flags import get_flags
from ..metrics import snapshot
from ..models import ACTIVE_BOOKING_STATUSES, Booking, Event, Seat, SeatStatus
from ..redis_client import get_redis
from ..schemas import EventOut, SeatOut

router = APIRouter(prefix="/events", tags=["events"])


async def _get_event(session: AsyncSession, event_id: int) -> Event:
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFound(f"Event {event_id} not found")
    return event


@router.get("", response_model=list[EventOut])
async def list_events(session: AsyncSession = Depends(get_session)) -> list[Event]:
    return list((await session.scalars(select(Event).order_by(Event.id.desc()))).all())


@router.get("/{event_id}", response_model=EventOut)
async def get_event(event_id: int, session: AsyncSession = Depends(get_session)) -> Event:
    return await _get_event(session, event_id)


@router.get("/{event_id}/seats", response_model=list[SeatOut])
async def list_seats(
    event_id: int,
    status: SeatStatus | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[Seat]:
    await _get_event(session, event_id)
    q = select(Seat).where(Seat.event_id == event_id).order_by(Seat.id)
    if status is not None:
        q = q.where(Seat.status == status)
    return list((await session.scalars(q)).all())


async def oversell_report(session: AsyncSession, event_id: int) -> dict[int, int]:
    """seat_id -> number of live tickets, for seats sold more than once.
    This is the number the whole project exists to drive to zero."""
    rows = await session.execute(
        select(Booking.seat_id, func.count())
        .where(Booking.event_id == event_id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .group_by(Booking.seat_id)
        .having(func.count() > 1)
    )
    return {seat_id: count for seat_id, count in rows.all()}


@router.get("/{event_id}/live")
async def live_view(event_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Everything the dashboard needs in one poll."""
    event = await _get_event(session, event_id)
    seats = (await session.execute(select(Seat.id, Seat.label, Seat.status).where(Seat.event_id == event_id).order_by(Seat.id))).all()
    oversold = await oversell_report(session, event_id)
    ticket_counts = dict(
        (
            await session.execute(
                select(Booking.mode, func.count())
                .where(Booking.event_id == event_id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
                .group_by(Booking.mode)
            )
        ).all()
    )

    counts = {s.value: 0 for s in SeatStatus}
    for _, _, status in seats:
        counts[status.value] += 1

    r = get_redis()
    flags = await get_flags(r)
    return {
        "event": EventOut.model_validate(event).model_dump(mode="json"),
        "seats": [[sid, label, status.value, oversold.get(sid, 1 if status != SeatStatus.AVAILABLE else 0)] for sid, label, status in seats],
        "seat_counts": counts,
        "tickets_sold": {mode.value: n for mode, n in ticket_counts.items()},
        "double_booked_seats": len(oversold),
        "extra_tickets_sold": sum(c - 1 for c in oversold.values()),
        "flags": flags.__dict__,
        "metrics": await snapshot(r),
    }
