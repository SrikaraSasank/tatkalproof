"""Lightweight live metrics kept in Redis so that every API worker contributes
to one shared view (requests/sec, latency percentiles, outcome counters)."""

import time

LATENCY_SAMPLES = 5000
SKIP_PREFIXES = ("/dashboard", "/static", "/events/", "/healthz", "/docs", "/openapi", "/favicon")


async def bump(r, name: str, amount: int = 1) -> None:
    try:
        await r.hincrby("m:counters", name, amount)
    except Exception:  # metrics must never break a booking
        pass


async def record_request(r, status: int, latency_ms: float) -> None:
    sec = int(time.time())
    try:
        pipe = r.pipeline(transaction=False)
        pipe.incr(f"m:rps:{sec}")
        pipe.expire(f"m:rps:{sec}", 120)
        pipe.lpush("m:lat", f"{latency_ms:.2f}")
        pipe.ltrim("m:lat", 0, LATENCY_SAMPLES - 1)
        pipe.hincrby("m:status", str(status), 1)
        await pipe.execute()
    except Exception:
        pass


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return round(sorted_vals[idx], 2)


async def snapshot(r) -> dict:
    now = int(time.time())
    pipe = r.pipeline(transaction=False)
    for sec in range(now - 10, now + 1):
        pipe.get(f"m:rps:{sec}")
    pipe.lrange("m:lat", 0, LATENCY_SAMPLES - 1)
    pipe.hgetall("m:status")
    pipe.hgetall("m:counters")
    results = await pipe.execute()
    rps_series = [int(v or 0) for v in results[:11]]
    latencies = sorted(float(x) for x in results[11])
    return {
        # the current second is still filling up, so report the last full one
        "rps": rps_series[-2],
        "rps_series": rps_series[:-1],
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "samples": len(latencies),
        },
        "status_codes": {k: int(v) for k, v in results[12].items()},
        "counters": {k: int(v) for k, v in results[13].items()},
    }


async def reset_metrics(r) -> None:
    keys = [k async for k in r.scan_iter(match="m:*", count=500)]
    if keys:
        await r.delete(*keys)


class MetricsMiddleware:
    """Pure ASGI middleware (cheaper than BaseHTTPMiddleware on hot paths)."""

    def __init__(self, app, redis_getter):
        self.app = app
        self.redis_getter = redis_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(SKIP_PREFIXES) or scope["path"] == "/":
            return await self.app(scope, receive, send)

        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            await record_request(self.redis_getter(), status_holder["status"], elapsed)
