"""The heart of the project: the naive booking flow (which double-books under
load) and the hardened flow (which can't).

Lock ordering rule, used by every path that touches both rows:
    booking row first, then seat row.
Keeping one global order means the payment path, the cancel path and the
expiry sweeper can never deadlock each other.
"""

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..config import get_settings
from ..errors import HoldExpired, InvalidState, NotFound, SeatUnavailable, TooManyHolds
from ..locks import acquire_seat_lock, release_seat_lock
from ..metrics import bump
from ..models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingMode,
    BookingStatus,
    Event,
    Seat,
    SeatStatus,
    as_utc,
    utcnow,
)


async def _load_seat(session: AsyncSession, event_id: int, seat_id: int) -> tuple[Event, Seat]:
    seat = await session.get(Seat, seat_id)
    if seat is None or seat.event_id != event_id:
        raise NotFound(f"Seat {seat_id} not found in event {event_id}")
    event = await session.get(Event, event_id)
    return event, seat


# --------------------------------------------------------------------------
# 1. NAIVE: check-then-act. Looks fine, passes every single-user test, and
#    double-books the moment two people click at the same time.
# --------------------------------------------------------------------------
async def book_seat_naive(session: AsyncSession, r, user: CurrentUser, event_id: int, seat_id: int) -> Booking:
    event, seat = await _load_seat(session, event_id, seat_id)

    if seat.status != SeatStatus.AVAILABLE:  # CHECK ...
        await bump(r, "naive_rejected")
        raise SeatUnavailable(f"Seat {seat.label} is already taken")

    # ... real systems do work here: pricing, quota checks, fraud scoring.
    # Every request that passed the check above is now "in flight" together.
    await asyncio.sleep(get_settings().naive_processing_delay_ms / 1000)

    seat.status = SeatStatus.BOOKED  # ... ACT (blind write: UPDATE seats SET status WHERE id=?)
    seat.held_by_user_id = user.id
    booking = Booking(
        event_id=event_id,
        seat_id=seat.id,
        seat_label=seat.label,
        user_id=user.id,
        mode=BookingMode.NAIVE,
        status=BookingStatus.CONFIRMED,
        amount=event.price,
        payment_ref=f"pay_{uuid.uuid4().hex[:16]}",
        confirmed_at=utcnow(),
    )
    session.add(booking)
    await session.commit()
    await bump(r, "naive_booked")
    return booking


# --------------------------------------------------------------------------
# 2. SAFE: hold -> pay -> confirm, with three independent layers of defence.
# --------------------------------------------------------------------------
async def hold_seat(session: AsyncSession, r, user: CurrentUser, event_id: int, seat_id: int) -> Booking:
    s = get_settings()
    event, seat = await _load_seat(session, event_id, seat_id)

    # Cheap early exit: most losers of a rush are rejected here without
    # touching Redis or taking any lock.
    if seat.status != SeatStatus.AVAILABLE:
        await bump(r, "hold_conflict")
        raise SeatUnavailable(f"Seat {seat.label} is not available")

    active_holds = await session.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.user_id == user.id,
            Booking.event_id == event_id,
            Booking.status == BookingStatus.HELD,
        )
    )
    if active_holds >= s.max_active_holds_per_user:
        raise TooManyHolds(f"You can hold at most {s.max_active_holds_per_user} seats at once")

    # Layer 1 - Redis lock (SET NX EX): an atomic gate in front of the DB, so
    # thousands of losers never even reach Postgres.
    owner = str(user.id)
    if not await acquire_seat_lock(r, event_id, seat_id, owner, s.hold_ttl_seconds):
        await bump(r, "hold_conflict")
        raise SeatUnavailable(f"Seat {seat.label} is being booked by someone else")

    try:
        # Layer 2 - conditional UPDATE (compare-and-set). Even if Redis were
        # flushed or failed over and lost the lock, the database itself only
        # lets ONE transaction move this row out of AVAILABLE.
        expires = utcnow() + timedelta(seconds=s.hold_ttl_seconds)
        result = await session.execute(
            update(Seat)
            .where(Seat.id == seat_id, Seat.event_id == event_id, Seat.status == SeatStatus.AVAILABLE)
            .values(
                status=SeatStatus.HELD,
                held_by_user_id=user.id,
                hold_expires_at=expires,
                version=Seat.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            await bump(r, "hold_conflict_db")
            raise SeatUnavailable(f"Seat {seat.label} is not available")

        booking = Booking(
            event_id=event_id,
            seat_id=seat_id,
            seat_label=seat.label,
            user_id=user.id,
            mode=BookingMode.SAFE,
            status=BookingStatus.HELD,
            amount=event.price,
            hold_expires_at=expires,
        )
        session.add(booking)
        await session.commit()
    except BaseException:
        await release_seat_lock(r, event_id, seat_id, owner)
        raise

    await bump(r, "hold_ok")
    return booking


async def _charge_payment_gateway(amount: int) -> str:
    """Stand-in for Razorpay/Stripe. Slow on purpose."""
    await asyncio.sleep(get_settings().payment_delay_ms / 1000)
    return f"pay_{uuid.uuid4().hex[:16]}"


async def pay_for_booking(session: AsyncSession, r, user: CurrentUser, booking_id: str) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.user_id != user.id:
        raise NotFound("Booking not found")
    if booking.status == BookingStatus.CONFIRMED:
        raise InvalidState("Booking is already paid (retry with the same Idempotency-Key to get the original receipt)")
    if booking.status != BookingStatus.HELD:
        raise InvalidState(f"Booking is {booking.status.value}, cannot pay")
    if as_utc(booking.hold_expires_at) <= utcnow():
        raise HoldExpired("Your hold expired before payment; please pick the seat again")

    # Charge OUTSIDE any DB transaction: never hold a pooled connection or a
    # row lock across a slow network call. Ending the read transaction here
    # returns the connection to the pool while the gateway thinks.
    amount = booking.amount
    await session.rollback()
    payment_ref = await _charge_payment_gateway(amount)

    # Layer 3 - pessimistic row locks for the final state transition.
    # Order: booking, then seat (see module docstring).
    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update().execution_options(populate_existing=True)
    )
    seat = await session.scalar(
        select(Seat).where(Seat.id == booking.seat_id).with_for_update().execution_options(populate_existing=True)
    )
    still_ours = (
        booking.status == BookingStatus.HELD
        and seat.status == SeatStatus.HELD
        and seat.held_by_user_id == user.id
        and as_utc(booking.hold_expires_at) > utcnow()
    )
    if not still_ours:
        await session.rollback()
        # Real system: issue a refund for payment_ref here.
        await bump(r, "pay_refunded")
        raise HoldExpired("Your hold expired while the payment was processing; the charge will be refunded")

    now = utcnow()
    seat.status = SeatStatus.BOOKED
    seat.hold_expires_at = None
    seat.version += 1
    booking.status = BookingStatus.CONFIRMED
    booking.payment_ref = payment_ref
    booking.confirmed_at = now
    booking.hold_expires_at = None
    await session.commit()

    # The DB row is now BOOKED, which is the real source of truth; drop the lock.
    await release_seat_lock(r, booking.event_id, booking.seat_id, str(user.id))
    await bump(r, "pay_ok")
    return booking


async def cancel_booking(session: AsyncSession, r, user: CurrentUser, booking_id: str) -> Booking:
    booking = await session.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    if booking is None or booking.user_id != user.id:
        raise NotFound("Booking not found")
    if booking.status not in ACTIVE_BOOKING_STATUSES:
        raise InvalidState(f"Booking is already {booking.status.value}")

    await session.execute(
        update(Seat)
        .where(Seat.id == booking.seat_id, Seat.held_by_user_id == user.id, Seat.status != SeatStatus.AVAILABLE)
        .values(status=SeatStatus.AVAILABLE, held_by_user_id=None, hold_expires_at=None, version=Seat.version + 1)
        .execution_options(synchronize_session=False)
    )
    booking.status = BookingStatus.CANCELLED
    booking.hold_expires_at = None
    await session.commit()
    await release_seat_lock(r, booking.event_id, booking.seat_id, str(user.id))
    await bump(r, "cancelled")
    return booking


# --------------------------------------------------------------------------
# 3. Expired-hold sweeper: users who hold a seat and walk away.
# --------------------------------------------------------------------------
async def release_expired_holds(session_factory, r, batch_size: int = 500) -> int:
    async with session_factory() as session:
        expired = (
            await session.execute(
                select(Booking.id, Booking.event_id, Booking.seat_id, Booking.user_id)
                .where(Booking.status == BookingStatus.HELD, Booking.hold_expires_at <= utcnow())
                .limit(batch_size)
            )
        ).all()

    released = 0
    for booking_id, event_id, seat_id, user_id in expired:
        # One short transaction per booking keeps locks brief.
        async with session_factory() as session:
            res = await session.execute(
                update(Booking)
                .where(Booking.id == booking_id, Booking.status == BookingStatus.HELD)
                .values(status=BookingStatus.EXPIRED, hold_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:  # paid or cancelled in the meantime
                await session.rollback()
                continue
            await session.execute(
                update(Seat)
                .where(Seat.id == seat_id, Seat.status == SeatStatus.HELD, Seat.held_by_user_id == user_id)
                .values(status=SeatStatus.AVAILABLE, held_by_user_id=None, hold_expires_at=None, version=Seat.version + 1)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        await release_seat_lock(r, event_id, seat_id, str(user_id))
        released += 1

    if released:
        await bump(r, "holds_expired", released)
    return released
