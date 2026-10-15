from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, create_access_token, get_current_user, hash_password, verify_password
from ..db import get_session
from ..models import User
from ..schemas import MeOut, RegisterIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user = User(username=body.username, password_hash=await hash_password(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")
    return TokenOut(access_token=create_access_token(user.id, user.username), user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenOut)
async def login(
    form: OAuth2PasswordRequestForm = Depends(), session: AsyncSession = Depends(get_session)
) -> TokenOut:
    user = await session.scalar(select(User).where(User.username == form.username))
    if user is None or not await verify_password(form.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(
        access_token=create_access_token(user.id, user.username, user.is_admin),
        user_id=user.id,
        username=user.username,
    )


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser = Depends(get_current_user)) -> MeOut:
    return MeOut(id=user.id, username=user.username)
