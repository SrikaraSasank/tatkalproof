from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    settings = get_settings()
    if url.startswith("sqlite"):
        # SQLite serialises writers; a generous busy timeout keeps the
        # test suite from failing with "database is locked".
        return create_async_engine(url, connect_args={"timeout": 30})
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


engine = _make_engine(get_settings().database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from . import models  # noqa: F401  (register tables on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
