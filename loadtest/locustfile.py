"""Locust scenarios for sustained load (rush.py is for the one-shot "10 AM" spike).

    locust -f loadtest/locustfile.py --host http://localhost:8000
    # then open http://localhost:8089, pick users + spawn rate

Set EVENT_ID to target a specific event (defaults to the newest one).
Run only one scenario with:  locust -f loadtest/locustfile.py SafeBooker
"""

import os
import random
import uuid

from locust import HttpUser, between, task

PASSWORD = "locustpass123"


class _Base(HttpUser):
    abstract = True
    wait_time = between(0.1, 0.5)

    def on_start(self):
        name = f"locust_{uuid.uuid4().hex[:12]}"
        res = self.client.post("/auth/register", json={"username": name, "password": PASSWORD}, name="/auth/register")
        self.headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
        event_id = os.getenv("EVENT_ID")
        if not event_id:
            event_id = self.client.get("/events", name="/events").json()[0]["id"]
        self.event_id = int(event_id)
        self.seat_ids = [s["id"] for s in self.client.get(f"/events/{self.event_id}/seats", name="/events/[id]/seats").json()]

    def pick_seat(self) -> int:
        return random.choice(self.seat_ids)


class NaiveBooker(_Base):
    @task
    def book(self):
        with self.client.post(
            "/naive/book", json={"event_id": self.event_id, "seat_id": self.pick_seat()},
            headers=self.headers, name="/naive/book", catch_response=True,
        ) as res:
            if res.status_code in (201, 409):
                res.success()


class SafeBooker(_Base):
    @task(5)
    def hold_and_pay(self):
        with self.client.post(
            "/bookings/hold", json={"event_id": self.event_id, "seat_id": self.pick_seat()},
            headers=self.headers, name="/bookings/hold", catch_response=True,
        ) as res:
            if res.status_code in (409, 429):
                res.success()
                return
            if res.status_code != 201:
                return
            booking_id = res.json()["id"]
        key = str(uuid.uuid4())
        for _ in range(2 if random.random() < 0.2 else 1):  # sometimes double-click Pay
            with self.client.post(
                f"/bookings/{booking_id}/pay", headers={**self.headers, "Idempotency-Key": key},
                name="/bookings/[id]/pay", catch_response=True,
            ) as pay:
                if pay.status_code in (200, 409, 410):
                    pay.success()

    @task(1)
    def browse(self):
        self.client.get(f"/events/{self.event_id}/seats?status=AVAILABLE", name="/events/[id]/seats?available")
