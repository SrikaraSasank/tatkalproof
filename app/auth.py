from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .models import utcnow

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    is_admin: bool = False


# bcrypt is deliberately slow and CPU-bound. Running it on the event loop would
# freeze every other request during a rush, so it goes to a thread pool.
async def hash_password(password: str) -> str:
    rounds = get_settings().bcrypt_rounds
    hashed = await run_in_threadpool(bcrypt.hashpw, password.encode(), bcrypt.gensalt(rounds))
    return hashed.decode()


async def verify_password(password: str, password_hash: str) -> bool:
    return await run_in_threadpool(bcrypt.checkpw, password.encode(), password_hash.encode())


def create_access_token(user_id: int, username: str, is_admin: bool = False) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),
        "name": username,
        "adm": is_admin,
        "exp": utcnow() + timedelta(minutes=s.jwt_expire_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> CurrentUser:
    """Stateless auth: the signed JWT is trusted, so a booking request costs
    zero extra DB queries for authentication. Matters under 10k req/s."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
        return CurrentUser(id=int(payload["sub"]), username=payload["name"], is_admin=bool(payload.get("adm")))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if x_admin_token != get_settings().admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin token required")
