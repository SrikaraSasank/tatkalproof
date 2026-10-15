"""Idempotency keys, the way Stripe does them.

The client sends `Idempotency-Key: <uuid>` with a payment. The first request
inserts an IN_PROGRESS row; the database's unique constraint on (user, key)
guarantees only one request can ever insert it. That request does the work and
stores the response. Every retry or double-click with the same key gets the
stored response back instead of charging the user again.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .errors import DomainError, IdempotencyConflict, IdempotencyMismatch
from .models import IdempotencyRecord, utcnow

Action = Callable[[], Awaitable[tuple[int, dict]]]


def request_fingerprint(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


async def run_idempotent(
    user_id: int, key: str, endpoint: str, request_hash: str, action: Action
) -> tuple[int, dict, bool]:
    """Returns (status_code, body, replayed)."""
    # Step 1: claim the key.
    async with SessionLocal() as session:
        session.add(
            IdempotencyRecord(user_id=user_id, key=key, endpoint=endpoint, request_hash=request_hash)
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(IdempotencyRecord).where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key)
            )
            if existing is None:  # row vanished (failed attempt cleaned up): let the client retry
                raise IdempotencyConflict("Previous attempt with this key failed; retry")
            if existing.request_hash != request_hash or existing.endpoint != endpoint:
                raise IdempotencyMismatch("This Idempotency-Key was already used for a different request")
            if existing.state != "COMPLETED":
                raise IdempotencyConflict(
                    "A request with this Idempotency-Key is still being processed",
                    headers={"Retry-After": "1"},
                )
            return existing.response_code, json.loads(existing.response_body), True

    # Step 2: we own the key, so do the work exactly once.
    try:
        status_code, body = await action()
    except DomainError as exc:
        # Business outcomes (seat gone, hold expired...) are final: store them
        # so retries see the same answer.
        status_code, body = exc.status_code, exc.body()
    except BaseException:
        # Unexpected failure (5xx): release the key so the client can retry.
        async with SessionLocal() as session:
            await session.execute(
                delete(IdempotencyRecord).where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key)
            )
            await session.commit()
        raise

    # Step 3: persist the response.
    async with SessionLocal() as session:
        await session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key)
            .values(state="COMPLETED", response_code=status_code, response_body=json.dumps(body))
        )
        await session.commit()
    return status_code, body, False


async def purge_old_keys(ttl_hours: int) -> int:
    async with SessionLocal() as session:
        res = await session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < utcnow() - timedelta(hours=ttl_hours))
        )
        await session.commit()
        return res.rowcount or 0
