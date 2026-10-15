import os
import tempfile
from pathlib import Path

# Configure BEFORE the app is imported: the engine and settings are created at import.
_tmp = Path(tempfile.mkdtemp(prefix="tatkal_test_"))
os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_tmp / 'test.db'}",
    REDIS_URL="fake://",
    BCRYPT_ROUNDS="4",
    RUN_BACKGROUND_WORKERS="false",
    AUTO_CREATE_TABLES="false",
    NAIVE_PROCESSING_DELAY_MS="40",
    PAYMENT_DELAY_MS="20",
    RATE_LIMIT_ENABLED="false",
    ADMIN_TOKEN="test-admin",
)

import uuid  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

ADMIN = {"X-Admin-Token": "test-admin"}


@pytest_asyncio.fixture(autouse=True)
async def fresh_state():
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await get_redis().flushall()
    yield


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        yield c


@pytest_asyncio.fixture
async def event(client):
    res = await client.post(
        "/admin/events",
        json={"name": "Test Express", "venue": "A -> B", "starts_at": "2030-01-01T10:00:00Z",
              "price": 1000, "rows": 2, "seats_per_row": 5},
        headers=ADMIN,
    )
    assert res.status_code == 201, res.text
    ev = res.json()
    seats = (await client.get(f"/events/{ev['id']}/seats")).json()
    return {"id": ev["id"], "seats": [s["id"] for s in seats]}


async def make_user(client) -> dict:
    res = await client.post("/auth/register", json={"username": f"u_{uuid.uuid4().hex[:10]}", "password": "secret123"})
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture
def user_factory(client):
    async def factory(n: int = 1):
        return [await make_user(client) for _ in range(n)]

    return factory
