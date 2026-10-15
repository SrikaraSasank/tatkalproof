from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user
from ..db import get_session
from ..models import Booking
from ..redis_client import get_redis
from ..schemas import BookingOut, HoldIn
from ..services import booking as svc

router = APIRouter(prefix="/naive", tags=["bookings (naive - intentionally broken)"])


@router.post("/book", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def naive_book(
    body: HoldIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Booking:
    """The "before" picture: check-then-act booking with no locking, no rate
    limit, no idempotency. Hit it concurrently and it sells one seat many times."""
    return await svc.book_seat_naive(session, get_redis(), user, body.event_id, body.seat_id)
