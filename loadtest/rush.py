"""Tatkal rush simulator.

Registers N users, holds them all behind a starting gun, then fires every
booking request at the same instant. Runs the naive endpoint, the hardened
endpoint, or both, and prints a before/after comparison.

    python loadtest/rush.py --mode both --users 2000
    python loadtest/rush.py --mode safe --users 5000 --hot-seats 20 --double-click-rate 0.3
"""

import argparse
import asyncio
import json
import random
import statistics
import string
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))], 1)


class Rush:
    def __init__(self, args):
        self.args = args
        limits = httpx.Limits(max_connections=args.max_connections, max_keepalive_connections=args.max_connections)
        self.client = httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(120.0), limits=limits)
        self.admin_headers = {"X-Admin-Token": args.admin_token}

    async def close(self):
        await self.client.aclose()

    # ---------------------------------------------------------------- setup
    async def create_event(self, label: str) -> int:
        body = {
            "name": f"Rush test ({label}) {datetime.now().strftime('%H:%M:%S')}",
            "venue": "Load test",
            "starts_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "price": 1500,
            "rows": self.args.rows,
            "seats_per_row": self.args.seats_per_row,
        }
        res = await self.client.post("/admin/events", json=body, headers=self.admin_headers)
        res.raise_for_status()
        return res.json()["id"]

    async def reset_event(self, event_id: int):
        (await self.client.post(f"/admin/events/{event_id}/reset", headers=self.admin_headers)).raise_for_status()

    async def seat_ids(self, event_id: int) -> list[int]:
        res = await self.client.get(f"/events/{event_id}/seats")
        res.raise_for_status()
        ids = [s["id"] for s in res.json()]
        if self.args.hot_seats:
            ids = ids[: self.args.hot_seats]
        return ids

    async def register_users(self, n: int) -> list[str]:
        run = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        sem = asyncio.Semaphore(self.args.register_concurrency)
        tokens: list[str] = []

        async def one(i: int):
            async with sem:
                res = await self.client.post("/auth/register", json={"username": f"rush_{run}_{i}", "password": "rushpass123"})
                res.raise_for_status()
                tokens.append(res.json()["access_token"])

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        print(f"  registered {n} users in {time.perf_counter() - t0:.1f}s")
        return tokens

    # ---------------------------------------------------------------- rush
    async def run_mode(self, mode: str, event_id: int, tokens: list[str]) -> dict:
        await self.reset_event(event_id)
        seats = await self.seat_ids(event_id)
        go = asyncio.Event()
        results: list[tuple[str, int, float]] = []  # (kind, status, ms)
        pay_refs: dict[str, set[str]] = {}

        async def timed(kind: str, coro):
            t0 = time.perf_counter()
            try:
                res = await coro
                status = res.status_code
            except httpx.HTTPError:
                res, status = None, 0
            results.append((kind, status, (time.perf_counter() - t0) * 1000))
            return res

        async def pay(headers, booking_id: str, key: str):
            res = await timed("pay", self.client.post(f"/bookings/{booking_id}/pay", headers={**headers, "Idempotency-Key": key}))
            if res is not None and res.status_code == 200:
                pay_refs.setdefault(booking_id, set()).add(res.json()["payment_ref"])

        async def user(token: str):
            headers = {"Authorization": f"Bearer {token}"}
            seat_id = random.choice(seats)
            await go.wait()
            if mode == "naive":
                await timed("book", self.client.post("/naive/book", json={"event_id": event_id, "seat_id": seat_id}, headers=headers))
                return
            res = await timed("hold", self.client.post("/bookings/hold", json={"event_id": event_id, "seat_id": seat_id}, headers=headers))
            if res is None or res.status_code != 201:
                return
            booking_id = res.json()["id"]
            key = str(uuid.uuid4())
            if random.random() < self.args.double_click_rate:
                # impatient user mashes "Pay" twice; same key both times
                await asyncio.gather(pay(headers, booking_id, key), pay(headers, booking_id, key))
            else:
                await pay(headers, booking_id, key)

        tasks = [asyncio.create_task(user(t)) for t in tokens]
        await asyncio.sleep(0.5)  # let every task reach the starting line
        await self.client.post("/admin/metrics/reset", headers=self.admin_headers)
        print(f"  [{mode}] {len(tokens)} users -> {len(seats)} seats... GO")
        t0 = time.perf_counter()
        go.set()
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0

        live = (await self.client.get(f"/events/{event_id}/live")).json()
        first = [r for r in results if r[0] in ("book", "hold")]
        statuses = Counter(s for _, s, _ in first)
        lat = [ms for _, _, ms in first]
        return {
            "mode": mode,
            "event_id": event_id,
            "users": len(tokens),
            "seats_in_play": len(seats),
            "wall_seconds": round(wall, 2),
            "requests": len(results),
            "throughput_rps": round(len(results) / wall, 1) if wall else None,
            "first_request_status": dict(sorted((str(k), v) for k, v in statuses.items())),
            "latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99),
                           "mean": round(statistics.fmean(lat), 1) if lat else None},
            "seats_booked": live["seat_counts"]["BOOKED"],
            "tickets_issued": sum(live["tickets_sold"].values()),
            "double_booked_seats": live["double_booked_seats"],
            "extra_tickets_sold": live["extra_tickets_sold"],
            "duplicate_charges": sum(1 for refs in pay_refs.values() if len(refs) > 1),
            "double_click_payments": sum(1 for k, _, _ in results if k == "pay") - len(pay_refs),
        }


def print_table(reports: list[dict]) -> None:
    rows = [
        ("Users / seats", lambda r: f"{r['users']} / {r['seats_in_play']}"),
        ("Wall time (s)", lambda r: r["wall_seconds"]),
        ("Throughput (req/s)", lambda r: r["throughput_rps"]),
        ("p50 / p95 / p99 (ms)", lambda r: f"{r['latency_ms']['p50']} / {r['latency_ms']['p95']} / {r['latency_ms']['p99']}"),
        ("Status codes", lambda r: ", ".join(f"{k}:{v}" for k, v in r["first_request_status"].items())),
        ("Seats booked", lambda r: r["seats_booked"]),
        ("Tickets issued", lambda r: r["tickets_issued"]),
        ("DOUBLE-BOOKED SEATS", lambda r: r["double_booked_seats"]),
        ("Extra tickets sold", lambda r: r["extra_tickets_sold"]),
        ("Duplicate charges", lambda r: r["duplicate_charges"] if r["mode"] == "safe" else "n/a"),
    ]
    w0 = max(len(name) for name, _ in rows) + 2
    cols = [str(r["mode"]).upper() for r in reports]
    print("\n" + "".ljust(w0) + "".join(c.ljust(28) for c in cols))
    print("-" * (w0 + 28 * len(cols)))
    for name, fn in rows:
        print(name.ljust(w0) + "".join(str(fn(r)).ljust(28) for r in reports))
    print()


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--admin-token", default="admin-secret")
    p.add_argument("--mode", choices=["naive", "safe", "both"], default="both")
    p.add_argument("--users", type=int, default=1000)
    p.add_argument("--event-id", type=int, help="reuse an existing event (it will be reset)")
    p.add_argument("--rows", type=int, default=10)
    p.add_argument("--seats-per-row", type=int, default=10)
    p.add_argument("--hot-seats", type=int, default=0, help="only fight over the first K seats (0 = all)")
    p.add_argument("--double-click-rate", type=float, default=0.2)
    p.add_argument("--max-connections", type=int, default=500)
    p.add_argument("--register-concurrency", type=int, default=50)
    p.add_argument("--out", default="loadtest/results")
    args = p.parse_args()

    rush = Rush(args)
    try:
        event_id = args.event_id or await rush.create_event(args.mode)
        print(f"Event #{event_id}. Watch it live at {args.base_url}/dashboard")
        tokens = await rush.register_users(args.users)
        modes = ["naive", "safe"] if args.mode == "both" else [args.mode]
        reports = []
        for mode in modes:
            reports.append(await rush.run_mode(mode, event_id, tokens))
            if mode != modes[-1]:
                await asyncio.sleep(3)
        print_table(reports)

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"rush_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(reports, indent=2))
        print(f"Saved {path}")
    finally:
        await rush.close()


if __name__ == "__main__":
    asyncio.run(main())
