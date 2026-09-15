"""Smoke test for `Scheduler`: starts every loop against real Postgres/
Redis with no live agent connection (nothing registered in
`AgentConnectionRegistry`, so every `WSAgentBroker` call the loops make
returns the same `None`/`()` a genuinely disconnected agent would), lets
them run a couple of scheduling turns, and confirms `start()`/`stop()`
don't hang or raise - each loop's own try/except is what's actually under
test here, not the trading logic underneath (already covered elsewhere)."""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.market.enums import Timeframe
from app.services.scheduler import Scheduler, SchedulerConfig
from app.transport.registry import AgentConnectionRegistry
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


async def test_start_and_stop_do_not_hang_or_raise(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()

    config = SchedulerConfig(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        symbol="XAUUSD",
        environment="demo",
        primary_tf=Timeframe.M15,
        context_timeframes=(),
        scan_interval_seconds=0.05,
        outbox_dispatch_interval_seconds=0.05,
        reconciliation_interval_seconds=0.05,
        candle_close_grace_ms=1500,
        global_trading_enabled=False,
        quote_stale_seconds=5,
    )
    scheduler = Scheduler(
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
        redis=redis_client,
        registry=AgentConnectionRegistry(),
        config=config,
    )

    scheduler.start()
    assert len(scheduler._tasks) == 5  # whitebox: proves every loop actually started
    await asyncio.sleep(0.3)  # a few scheduling turns for each loop
    for task in scheduler._tasks:
        assert not task.done()  # still running its loop, not crashed out

    await scheduler.stop()

    assert scheduler._tasks == []
