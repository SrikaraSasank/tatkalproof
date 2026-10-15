from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """Business-rule failure that maps cleanly onto an HTTP response."""

    status_code = 400
    code = "bad_request"

    def __init__(self, detail: str | None = None, *, headers: dict[str, str] | None = None):
        self.detail = detail or self.code.replace("_", " ")
        self.headers = headers or {}
        super().__init__(self.detail)

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class SeatUnavailable(DomainError):
    status_code = 409
    code = "seat_unavailable"


class TooManyHolds(DomainError):
    status_code = 409
    code = "too_many_holds"


class InvalidState(DomainError):
    status_code = 409
    code = "invalid_state"


class HoldExpired(DomainError):
    status_code = 410
    code = "hold_expired"


class NotAdmitted(DomainError):
    status_code = 403
    code = "not_admitted"


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"


class IdempotencyConflict(DomainError):
    status_code = 409
    code = "idempotency_conflict"


class IdempotencyMismatch(DomainError):
    status_code = 422
    code = "idempotency_key_reused"


async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)
