from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


# pool_size небольшой: сервер общий с ботом, у Postgres кабинета
# max_connections урезан (см. docker-compose.yml). SQLite — только в тестах.
_engine_kwargs = (
    {"poolclass": NullPool}
    if DATABASE_URL.startswith("sqlite")
    else {"pool_size": 5, "max_overflow": 5}
)
engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    # Создаёт недостающие таблицы. Схема сразу описана на все три этапа,
    # поэтому новые таблицы позже добавлять не придётся; если понадобится
    # новый столбец в существующей таблице — это отдельная миграция.
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
