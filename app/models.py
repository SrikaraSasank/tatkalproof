import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _enum(cls):
    # Store enums as plain strings so the schema is portable (Postgres + SQLite).
    return Enum(cls, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class SeatStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    BOOKED = "BOOKED"


class BookingStatus(str, enum.Enum):
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class BookingMode(str, enum.Enum):
    NAIVE = "NAIVE"
    SAFE = "SAFE"


ACTIVE_BOOKING_STATUSES = (BookingStatus.HELD, BookingStatus.CONFIRMED)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """A train / show / match: anything with a fixed set of seats."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    venue: Mapped[str] = mapped_column(String(120))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price: Mapped[int] = mapped_column(Integer)  # rupees
    total_seats: Mapped[int] = mapped_column(Integer)
    seats_per_row: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("event_id", "label", name="uq_seat_event_label"),
        Index("ix_seats_event_status", "event_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(8))
    status: Mapped[SeatStatus] = mapped_column(_enum(SeatStatus), default=SeatStatus.AVAILABLE)
    held_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped on every state change: handy for debugging and optimistic checks.
    version: Mapped[int] = mapped_column(Integer, default=0)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_event_seat_status", "event_id", "seat_id", "status"),
        Index("ix_bookings_status_expiry", "status", "hold_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id", ondelete="CASCADE"))
    seat_label: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[BookingMode] = mapped_column(_enum(BookingMode))
    status: Mapped[BookingStatus] = mapped_column(_enum(BookingStatus))
    amount: Mapped[int] = mapped_column(Integer)
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    """One row per (user, Idempotency-Key). The unique constraint is what makes
    two simultaneous "Pay" clicks impossible to both execute."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="IN_PROGRESS")  # IN_PROGRESS | COMPLETED
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
