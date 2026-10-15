from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user
from ..db import SessionLocal, get_session
from ..errors import NotFound
from ..flags import get_flags
from ..idempotency import request_fingerprint, run_idempotent
from ..models import Booking
from ..ratelimit import booking_rate_limit
from ..redis_client import get_redis
from ..schemas import BookingOut, HoldIn
from ..services import booking as svc
from ..waiting_room import ensure_admitted

router = APIRouter(prefix="/bookings", tags=["bookings (safe)"])


@router.post("/hold", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def hold(
    body: HoldIn,
    user: CurrentUser = Depends(booking_rate_limit),
    session: AsyncSession = Depends(get_session),
) -> Booking:
    """Step 1: grab a seat for `HOLD_TTL_SECONDS` (default 5 min)."""
    r = get_redis()
    if (await get_flags(r)).waiting_room_enabled:
        await ensure_admitted(r, body.event_id, user.id)
    return await svc.hold_seat(session, r, user, body.event_id, body.seat_id)


@router.post(
    "/{booking_id}/pay",
    response_model=BookingOut,
    responses={409: {"description": "Already paid / request with same key in flight"}, 410: {"description": "Hold expired"}},
)
async def pay(
    booking_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: CurrentUser = Depends(booking_rate_limit),
) -> JSONResponse:
    """Step 2: pay and confirm. **Requires an `Idempotency-Key` header**:
    retrying or double-clicking with the same key never charges twice."""
    r = get_redis()

    async def action() -> tuple[int, dict]:
        async with SessionLocal() as session:
            booking = await svc.pay_for_booking(session, r, user, booking_id)
            return 200, BookingOut.model_validate(booking).model_dump(mode="json")

    code, body, replayed = await run_idempotent(
        user.id, idempotency_key, "POST /bookings/{id}/pay", request_fingerprint("pay", booking_id), action
    )
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(body, status_code=code, headers=headers)


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel(
    booking_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Booking:
    return await svc.cancel_booking(session, get_redis(), user, booking_id)


@router.get("/me", response_model=list[BookingOut])
async def my_bookings(
    user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[Booking]:
    q = select(Booking).where(Booking.user_id == user.id).order_by(Booking.created_at.desc()).limit(100)
    return list((await session.scalars(q)).all())


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: str, user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.user_id != user.id:
        raise NotFound("Booking not found")
    return booking
