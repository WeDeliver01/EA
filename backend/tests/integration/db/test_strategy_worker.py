"""SPEC-06 §5 steps 1-9 acceptance for `StrategyWorker`: WAIT is recorded
and produces no signal, TRADE persists a signal and queues it, a
context-timeframe close is a no-op, and an already-locked (account,
symbol, timeframe) is skipped rather than double-processed - all via the
real `stream:bars:closed` -> `stream:intents:pending` path against real
Postgres and Redis.

Uses a fake `StrategyEngine` rather than a real trade-ready bar sequence:
`StrategyWorker`'s job under test is orchestration (build state, call the
engine, persist, publish), not strategy logic, which the golden/
determinism tests already cover.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.streams import (
    STREAM_BARS_CLOSED,
    STREAM_INTENTS_PENDING,
    ensure_consumer_group,
    xadd,
)
from app.domain.market.bar import Bar
from app.domain.market.enums import Direction, Regime, Timeframe
from app.domain.market.market_state import MarketState
from app.domain.strategy.decision import Decision
from app.domain.strategy.enums import DecisionOutcome
from app.repositories.market_data import MarketDataRepository
from app.workers.strategy_worker import CONSUMER_GROUP, StrategyWorker, StrategyWorkerConfig
from tests.integration.seed import seed_minimal_refs

pytestmark = pytest.mark.integration


def _bar(open_time: datetime) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=open_time,
        open=Decimal("99.50"),
        high=Decimal("100.50"),
        low=Decimal("99.00"),
        close=Decimal("100.00"),
        tick_volume=100,
        real_volume=None,
        spread_points=2,
    )


def _decision(outcome: DecisionOutcome, *, strategy_version_id: UUID) -> Decision:
    trade_fields = (
        {
            "direction": Direction.LONG,
            "entry": Decimal("100.00"),
            "stop_loss": Decimal("99.50"),
        }
        if outcome is DecisionOutcome.TRADE
        else {"direction": None, "entry": None, "stop_loss": None}
    )
    return Decision(
        outcome=outcome,
        symbol="XAUUSD",
        as_of=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        strategy_version_id=strategy_version_id,
        regime=Regime.EXPANSION,
        setup=None,
        take_profits=(),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="test",
        engine_duration_ms=0,
        **trade_fields,
    )


class _FakeEngine:
    def __init__(self, decision: Decision) -> None:
        self._decision = decision
        self.calls: list[MarketState] = []

    def evaluate(self, state: MarketState) -> Decision:
        self.calls.append(state)
        return self._decision


async def _seed_and_upsert_primary_bar(db_session: AsyncSession, *, as_of: datetime):
    refs = await seed_minimal_refs(db_session)
    await db_session.commit()
    await MarketDataRepository(db_session).upsert_bars(
        [_bar(as_of - timedelta(minutes=15))],
        instrument_id=refs.instrument_id,
        source="mt5",
        ingested_at=as_of,
    )
    await db_session.commit()
    return refs


def _bars_closed_fields(
    *, account_id: UUID, instrument_id: UUID, symbol: str, timeframe: Timeframe, as_of: datetime
) -> dict[str, str]:
    return {
        "account_id": str(account_id),
        "instrument_id": str(instrument_id),
        "symbol": symbol,
        "timeframe": timeframe.value,
        "as_of": as_of.isoformat(),
    }


def _worker(
    *,
    redis: Redis,
    db_engine: AsyncEngine,
    engine: object,
    environment: str = "demo",
    consumer: str = "test-consumer",
) -> StrategyWorker:
    return StrategyWorker(
        redis=redis,
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
        engine=engine,  # type: ignore[arg-type]
        config=StrategyWorkerConfig(
            primary_tf=Timeframe.M15, context_timeframes=(), environment=environment
        ),
        consumer_name=consumer,
    )


async def _xpending_count(redis: Redis) -> int:
    info = await redis.xpending(STREAM_BARS_CLOSED, CONSUMER_GROUP)
    return int(info["pending"])


async def test_wait_is_recorded_and_produces_no_signal(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    refs = await _seed_and_upsert_primary_bar(db_session, as_of=as_of)

    fake_engine = _FakeEngine(
        _decision(DecisionOutcome.WAIT, strategy_version_id=refs.strategy_version_id)
    )
    worker = _worker(redis=redis_client, db_engine=db_engine, engine=fake_engine)

    await ensure_consumer_group(redis_client, STREAM_BARS_CLOSED, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_BARS_CLOSED,
        _bars_closed_fields(
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            timeframe=Timeframe.M15,
            as_of=as_of,
        ),
    )

    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert len(fake_engine.calls) == 1
    assert await _xpending_count(redis_client) == 0

    # seed_minimal_refs itself seeds a scaffolding analysis_run (mode="paper",
    # outcome="TRADE") for other tests' use - filter by mode="live" (what
    # this worker always writes) to see only what it actually did.
    rows = await db_session.execute(
        text("SELECT outcome FROM analysis_runs WHERE account_id = :a AND mode = 'live'"),
        {"a": str(refs.account_id)},
    )
    outcomes = [r[0] for r in rows]
    assert outcomes == ["WAIT"]

    signal_rows = await db_session.execute(
        text("SELECT count(*) FROM signals WHERE account_id = :a"), {"a": str(refs.account_id)}
    )
    assert signal_rows.scalar_one() == 0

    intents_len = await redis_client.xlen(STREAM_INTENTS_PENDING)
    assert intents_len == 0


async def test_trade_persists_a_signal_and_publishes_intent(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    refs = await _seed_and_upsert_primary_bar(db_session, as_of=as_of)

    fake_engine = _FakeEngine(
        _decision(DecisionOutcome.TRADE, strategy_version_id=refs.strategy_version_id)
    )
    worker = _worker(redis=redis_client, db_engine=db_engine, engine=fake_engine)

    await ensure_consumer_group(redis_client, STREAM_BARS_CLOSED, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_BARS_CLOSED,
        _bars_closed_fields(
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            timeframe=Timeframe.M15,
            as_of=as_of,
        ),
    )

    processed = await worker.run_once(block_ms=100)
    assert processed == 1

    signal_rows = await db_session.execute(
        text("SELECT id FROM signals WHERE account_id = :a"), {"a": str(refs.account_id)}
    )
    signal_ids = [str(r[0]) for r in signal_rows]
    assert len(signal_ids) == 1

    entries = await redis_client.xrange(STREAM_INTENTS_PENDING)
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    assert fields["signal_id"] == signal_ids[0]
    assert fields["account_id"] == str(refs.account_id)
    assert fields["environment"] == "demo"


async def test_context_timeframe_close_is_a_noop(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    refs = await _seed_and_upsert_primary_bar(db_session, as_of=as_of)

    fake_engine = _FakeEngine(
        _decision(DecisionOutcome.TRADE, strategy_version_id=refs.strategy_version_id)
    )
    worker = _worker(redis=redis_client, db_engine=db_engine, engine=fake_engine)

    await ensure_consumer_group(redis_client, STREAM_BARS_CLOSED, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_BARS_CLOSED,
        _bars_closed_fields(
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            timeframe=Timeframe.H1,  # worker's primary_tf is M15
            as_of=as_of,
        ),
    )

    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert fake_engine.calls == []
    assert await _xpending_count(redis_client) == 0

    # Only the scaffolding row seed_minimal_refs itself creates (mode="paper")
    # should exist - none from this worker, which never got past the
    # timeframe check to write anything.
    rows = await db_session.execute(
        text("SELECT count(*) FROM analysis_runs WHERE account_id = :a AND mode = 'live'"),
        {"a": str(refs.account_id)},
    )
    assert rows.scalar_one() == 0


async def test_an_already_held_lock_is_skipped_not_reprocessed(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    refs = await _seed_and_upsert_primary_bar(db_session, as_of=as_of)

    fake_engine = _FakeEngine(
        _decision(DecisionOutcome.TRADE, strategy_version_id=refs.strategy_version_id)
    )
    worker = _worker(redis=redis_client, db_engine=db_engine, engine=fake_engine)

    lock_key = f"lock:analyse:{refs.account_id}:XAUUSD:M15"
    held = await redis_client.set(lock_key, "someone-else", nx=True, ex=30)
    assert held  # sanity: we actually took the lock

    await ensure_consumer_group(redis_client, STREAM_BARS_CLOSED, CONSUMER_GROUP)
    await xadd(
        redis_client,
        STREAM_BARS_CLOSED,
        _bars_closed_fields(
            account_id=refs.account_id,
            instrument_id=refs.instrument_id,
            symbol="XAUUSD",
            timeframe=Timeframe.M15,
            as_of=as_of,
        ),
    )

    processed = await worker.run_once(block_ms=100)

    assert processed == 1
    assert fake_engine.calls == []  # never got past the lock check
    assert await _xpending_count(redis_client) == 0
