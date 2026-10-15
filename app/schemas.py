from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import BookingMode, BookingStatus, SeatStatus


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str


class MeOut(BaseModel):
    id: int
    username: str


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    price: int = Field(gt=0, le=1_000_000)
    rows: int = Field(default=10, ge=1, le=26)
    seats_per_row: int = Field(default=10, ge=1, le=50)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    venue: str
    starts_at: datetime
    price: int
    total_seats: int
    seats_per_row: int


class SeatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    status: SeatStatus


class HoldIn(BaseModel):
    event_id: int
    seat_id: int


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_id: int
    seat_id: int
    seat_label: str
    user_id: int
    mode: BookingMode
    status: BookingStatus
    amount: int
    payment_ref: str | None
    hold_expires_at: datetime | None
    created_at: datetime
    confirmed_at: datetime | None


class QueueStatusOut(BaseModel):
    event_id: int
    admitted: bool
    position: int | None = None
    pass_expires_in: int | None = None
    waiting_room_enabled: bool


class FlagsIn(BaseModel):
    rate_limit_enabled: bool | None = None
    rate_limit_per_second: int | None = Field(default=None, ge=1, le=10_000)
    waiting_room_enabled: bool | None = None
    waiting_room_admit_per_second: int | None = Field(default=None, ge=1, le=100_000)
