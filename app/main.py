import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import text

from . import __version__
from .config import get_settings
from .db import engine, init_db
from .errors import DomainError, domain_error_handler
from .metrics import MetricsMiddleware
from .redis_client import close_redis, get_redis
from .routers import admin, auth, bookings, events, naive, queue
from .worker import start_background_tasks, stop_background_tasks

STATIC_DIR = Path(__file__).parent / "static"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if settings.auto_create_tables:
        await init_db()
    background = start_background_tasks() if settings.run_background_workers else None
    yield
    if background:
        await stop_background_tasks(*background)
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="TatkalProof",
    version=__version__,
    description=(
        "A ticket-booking REST API built to survive an IRCTC Tatkal-style rush.\n\n"
        "* `/naive/book` is the intentionally broken check-then-act version.\n"
        "* `/bookings/hold` + `/bookings/{id}/pay` is the hardened version: Redis locks, "
        "compare-and-set updates, row locks, idempotent payments, rate limiting and a waiting room.\n\n"
        "Live dashboard: [/dashboard](/dashboard)"
    ),
    lifespan=lifespan,
)
app.add_middleware(MetricsMiddleware, redis_getter=get_redis)
app.add_exception_handler(DomainError, domain_error_handler)

for r in (auth.router, events.router, bookings.router, naive.router, queue.router, admin.router):
    app.include_router(r)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await get_redis().ping()
    return {"status": "ok", "version": __version__}
