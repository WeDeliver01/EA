"""Integration test fixtures: a real async SQLAlchemy session against the
local Postgres instance (see SPEC-10 Phase 1: alembic upgrade/downgrade,
append-only enforcement, and the state-transition trigger all need a real
database - they cannot be faked with mocks without testing nothing)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://dt:dt_dev_password@localhost:5432/delicate_trader"
)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    # Function-scoped: pytest-asyncio's default "auto" mode gives each test
    # its own event loop, and an asyncpg connection pool cannot be reused
    # across event loops (it raises "another operation is in progress").
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()

    # Real commits inside a test (needed to exercise the deferred trigger and
    # the append-only RULEs) are cleaned up here rather than relying on
    # transactional rollback, which a DEFERRABLE INITIALLY DEFERRED trigger
    # would never actually fire under.
    async with db_engine.begin() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename NOT IN "
                        "('alembic_version') AND tablename NOT LIKE 'market_bars_%' "
                        "AND tablename NOT LIKE 'market_ticks_%'"
                    )
                )
            )
            .scalars()
            .all()
        )
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
