import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.db import SessionLocal
from app.models import Booking, utcnow
from app.redis_client import get_redis
from app.services.booking import release_expired_holds
from app.waiting_room import admit_batch

from .conftest import ADMIN

pytestmark = pytest.mark.asyncio


async def hold(client, headers, event_id, seat_id):
    return await client.post("/bookings/hold", json={"event_id": event_id, "seat_id": seat_id}, headers=headers)


async def pay(client, headers, booking_id, key=None):
    key = key or str(uuid.uuid4())
    return await client.post(f"/bookings/{booking_id}/pay", headers={**headers, "Idempotency-Key": key})


async def live(client, event_id):
    return (await client.get(f"/events/{event_id}/live")).json()


# ------------------------------------------------------------------ auth
async def test_register_login_me(client):
    res = await client.post("/auth/register", json={"username": "sasank", "password": "secret123"})
    assert res.status_code == 201
    assert (await client.post("/auth/register", json={"username": "sasank", "password": "x" * 8})).status_code == 409

    res = await client.post("/auth/login", data={"username": "sasank", "password": "secret123"})
    assert res.status_code == 200
    token = res.json()["access_token"]
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "sasank"

    assert (await client.post("/auth/login", data={"username": "sasank", "password": "wrong!!"})).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_admin_requires_token(client):
    res = await client.post("/admin/events", json={})
    assert res.status_code == 403


# ------------------------------------------------------------------ happy path
async def test_hold_then_pay(client, event, user_factory):
    (u,) = await user_factory(1)
    res = await hold(client, u, event["id"], event["seats"][0])
    assert res.status_code == 201, res.text
    booking = res.json()
    assert booking["status"] == "HELD" and booking["hold_expires_at"]

    res = await pay(client, u, booking["id"])
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CONFIRMED" and res.json()["payment_ref"].startswith("pay_")

    data = await live(client, event["id"])
    assert data["seat_counts"]["BOOKED"] == 1 and data["double_booked_seats"] == 0


async def test_cannot_hold_taken_seat(client, event, user_factory):
    a, b = await user_factory(2)
    assert (await hold(client, a, event["id"], event["seats"][0])).status_code == 201
    res = await hold(client, b, event["id"], event["seats"][0])
    assert res.status_code == 409 and res.json()["error"] == "seat_unavailable"


async def test_cancel_frees_seat(client, event, user_factory):
    a, b = await user_factory(2)
    booking = (await hold(client, a, event["id"], event["seats"][0])).json()
    assert (await client.post(f"/bookings/{booking['id']}/cancel", headers=a)).json()["status"] == "CANCELLED"
    assert (await hold(client, b, event["id"], event["seats"][0])).status_code == 201


async def test_max_active_holds(client, event, user_factory):
    (u,) = await user_factory(1)
    for seat in event["seats"][:4]:
        assert (await hold(client, u, event["id"], seat)).status_code == 201
    res = await hold(client, u, event["id"], event["seats"][4])
    assert res.status_code == 409 and res.json()["error"] == "too_many_holds"


async def test_cannot_pay_for_someone_elses_booking(client, event, user_factory):
    a, b = await user_factory(2)
    booking = (await hold(client, a, event["id"], event["seats"][0])).json()
    assert (await pay(client, b, booking["id"])).status_code == 404


# ------------------------------------------------------------------ the race
async def test_naive_flow_double_books_under_concurrency(client, event, user_factory):
    """Proves the bug exists: 20 people, 1 seat, many tickets."""
    users = await user_factory(20)
    seat = event["seats"][0]
    results = await asyncio.gather(
        *(client.post("/naive/book", json={"event_id": event["id"], "seat_id": seat}, headers=u) for u in users)
    )
    winners = [r for r in results if r.status_code == 201]
    assert len(winners) > 1, "expected the naive flow to oversell"
    data = await live(client, event["id"])
    assert data["double_booked_seats"] == 1
    assert data["extra_tickets_sold"] == len(winners) - 1


async def test_safe_flow_never_double_books(client, event, user_factory):
    """Same rush against the hardened flow: exactly one winner per seat."""
    users = await user_factory(30)
    hot = event["seats"][:3]
    results = await asyncio.gather(*(hold(client, u, event["id"], hot[i % 3]) for i, u in enumerate(users)))
    codes = [r.status_code for r in results]
    assert codes.count(201) == 3
    assert codes.count(409) == 27
    data = await live(client, event["id"])
    assert data["double_booked_seats"] == 0 and data["seat_counts"]["HELD"] == 3


async def test_db_guard_holds_even_if_redis_loses_the_lock(client, event, user_factory):
    """Layer 2: wipe Redis between two holds (simulating a failover). The
    conditional UPDATE in the database must still reject the second one."""
    a, b = await user_factory(2)
    assert (await hold(client, a, event["id"], event["seats"][0])).status_code == 201
    r = get_redis()
    for key in [k async for k in r.scan_iter(match="seat:*")]:
        await r.delete(key)
    res = await hold(client, b, event["id"], event["seats"][0])
    assert res.status_code == 409
    assert (await live(client, event["id"]))["double_booked_seats"] == 0


# ------------------------------------------------------------------ idempotency
async def test_pay_retry_with_same_key_replays_response(client, event, user_factory):
    (u,) = await user_factory(1)
    booking = (await hold(client, u, event["id"], event["seats"][0])).json()
    key = str(uuid.uuid4())
    first = await pay(client, u, booking["id"], key)
    second = await pay(client, u, booking["id"], key)
    assert first.status_code == second.status_code == 200
    assert first.json()["payment_ref"] == second.json()["payment_ref"]
    assert second.headers.get("Idempotent-Replayed") == "true"

    # A NEW key on an already-paid booking must not charge again.
    third = await pay(client, u, booking["id"], str(uuid.uuid4()))
    assert third.status_code == 409


async def test_double_click_pay_charges_once(client, event, user_factory):
    (u,) = await user_factory(1)
    booking = (await hold(client, u, event["id"], event["seats"][0])).json()
    key = str(uuid.uuid4())
    results = await asyncio.gather(*(pay(client, u, booking["id"], key) for _ in range(5)))
    refs = {r.json()["payment_ref"] for r in results if r.status_code == 200}
    assert len(refs) == 1
    assert all(r.status_code in (200, 409) for r in results)


async def test_idempotency_key_cannot_be_reused_for_other_booking(client, event, user_factory):
    (u,) = await user_factory(1)
    b1 = (await hold(client, u, event["id"], event["seats"][0])).json()
    b2 = (await hold(client, u, event["id"], event["seats"][1])).json()
    key = str(uuid.uuid4())
    assert (await pay(client, u, b1["id"], key)).status_code == 200
    assert (await pay(client, u, b2["id"], key)).status_code == 422


async def test_missing_idempotency_key_rejected(client, event, user_factory):
    (u,) = await user_factory(1)
    booking = (await hold(client, u, event["id"], event["seats"][0])).json()
    assert (await client.post(f"/bookings/{booking['id']}/pay", headers=u)).status_code == 422


# ------------------------------------------------------------------ expiry
async def test_expired_hold_is_released_and_cannot_be_paid(client, event, user_factory):
    a, b = await user_factory(2)
    booking = (await hold(client, a, event["id"], event["seats"][0])).json()
    async with SessionLocal() as s:
        await s.execute(update(Booking).where(Booking.id == booking["id"]).values(hold_expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()

    assert (await pay(client, a, booking["id"])).status_code == 410
    assert await release_expired_holds(SessionLocal, get_redis()) == 1
    assert (await client.get(f"/bookings/{booking['id']}", headers=a)).json()["status"] == "EXPIRED"
    assert (await hold(client, b, event["id"], event["seats"][0])).status_code == 201


# ------------------------------------------------------------------ traffic control
async def test_rate_limit(client, event, user_factory):
    (u,) = await user_factory(1)
    await client.put("/admin/flags", json={"rate_limit_enabled": True, "rate_limit_per_second": 3}, headers=ADMIN)
    results = await asyncio.gather(*(hold(client, u, event["id"], event["seats"][i % 10]) for i in range(12)))
    limited = [r for r in results if r.status_code == 429]
    assert limited and "Retry-After" in limited[0].headers


async def test_waiting_room(client, event, user_factory):
    (u,) = await user_factory(1)
    await client.put("/admin/flags", json={"waiting_room_enabled": True}, headers=ADMIN)

    assert (await hold(client, u, event["id"], event["seats"][0])).status_code == 403
    joined = (await client.post(f"/queue/{event['id']}/join", headers=u)).json()
    assert joined["admitted"] is False and joined["position"] == 1

    await admit_batch(get_redis(), admit_per_event=10, pass_ttl=60)
    status = (await client.get(f"/queue/{event['id']}/status", headers=u)).json()
    assert status["admitted"] is True
    assert (await hold(client, u, event["id"], event["seats"][0])).status_code == 201


async def test_admin_reset(client, event, user_factory):
    (u,) = await user_factory(1)
    await hold(client, u, event["id"], event["seats"][0])
    res = await client.post(f"/admin/events/{event['id']}/reset", headers=ADMIN)
    assert res.json()["bookings_deleted"] == 1
    assert (await live(client, event["id"]))["seat_counts"]["AVAILABLE"] == 10
