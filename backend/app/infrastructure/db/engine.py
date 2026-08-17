"""Async SQLAlchemy engine and session factory.

A single ``async_sessionmaker`` is exposed as the application-wide
session factory.  FastAPI dependencies use ``get_db_session()`` to
obtain a per-request session that is automatically closed.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.registry.settings import settings

# Pre-R3 shared engine: SQLAlchemy/asyncpg DEFAULT JSON/JSONB semantics (120
# OPTION_B_COLUMN_LOCAL_CODEC).  The engine must NOT install a global
# json_serializer/json_deserializer — the R3 engine-wide exact codec changed
# unrelated JSONB behavior ({True: "x"} keys, mixed-key sort TypeError, tuple
# keys, NaN/Infinity failure stage, Decimal).  Only the LocalAgent sidecar
# ``attributes`` column uses the column-local exact JSONB bridge
# (``LocalAgentAttributesJSONB`` in app/infrastructure/db/types/).
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.POSTGRES_POOL_SIZE,
    max_overflow=settings.POSTGRES_MAX_OVERFLOW,
    echo=False,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a scoped async session, rolling back on unhandled errors."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
