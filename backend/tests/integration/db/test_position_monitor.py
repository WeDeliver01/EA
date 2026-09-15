"""SPEC-06 §8 acceptance for `PositionMonitorLoop`: real intent-backed
positions get found (`PositionRepository.list_open_for_management`), a
missing or stale live quote makes the whole tick a no-op, and a genuinely
favorable price move drives a real `PositionManager.apply()` call through
to the broker and the database - the orchestration this loop exists to
provide on top of already-tested pieces (`position_manager.decide()`,
`app.core.quotes`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.quotes import store_quote
from app.domain.execution.enums import OrderSide
from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Direction, Regime, Timeframe
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.engines.config import TradeConstructionConfig
from app.execution.broker import AsyncSimulatedBrokerAdapter, SimulatedBroker
from app.execution.dispatcher import OutboxDispatcher
from app.execution.event_consumer import EventConsumer
from app.execution.intent_service import submit_decision
from app.models.tables import Account, PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository, simulated_agent_id
from app.repositories.deals import DealRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.positions import PositionRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository
from app.services.position_monitor import PositionMonitorConfig, PositionMonitorLoop
from tests.integration.seed import SeededRefs, seed_minimal_refs, seed_signal

pytestmark = pytest.mark.integration

_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=10,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)

_ENTRY = Decimal("3418.20")
_STOP = Decimal("3412.55")  # initial_risk_price = 5.65


async def _open_managed_position(
    db_session: AsyncSession, refs: SeededRefs
) -> tuple[SimulatedBroker, uuid.UUID]:
    """Opens a real position through the actual submit -> dispatch -> fill
    pipeline (not a hand-built `LivePosition`) so `trade_intent_id` and
    `initial_stop_loss` land the way they would in production - this is
    exactly what `list_open_for_management`'s join depends on."""
    account = await db_session.get(Account, refs.account_id)
    assert account is not None
    account.trading_enabled = True
    account.kill_switch_active = False
    await db_session.commit()

    signal_id = await seed_signal(db_session, refs, reference=f"SIG-PMON-{uuid.uuid4().hex[:8]}")
    now = datetime.now(UTC)
    decision = Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=now,
        strategy_version_id=uuid.uuid4(),
        regime=Regime.TRENDING_UP,
        setup=None,
        direction=Direction.LONG,
        entry=_ENTRY,
        stop_loss=_STOP,
        take_profits=(
            TakeProfit(
                level=Decimal("3450.00"), fraction=Decimal("1.0"), r_multiple=Decimal("5.6")
            ),
        ),
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        evidence=(),
        gates=(),
        narrative="trade",
        engine_duration_ms=0,
    )
    result = await submit_decision(
        decision,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        signal_id=signal_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        environment="demo",
        as_of=now,
    )
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    dispatcher = OutboxDispatcher(
        broker=AsyncSimulatedBrokerAdapter(broker),
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    outcomes = await dispatcher.dispatch_pending(account_id=refs.account_id, as_of=now)
    await db_session.commit()
    assert outcomes[0].order_result is not None

    consumer = EventConsumer(
        agent_event_repo=AgentEventRepository(db_session),
        deal_repo=DealRepository(db_session),
        position_repo=PositionRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
    )
    await consumer.process_order_result(
        outcomes[0].order_result,
        trade_intent_id=result.trade_intent_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        agent_id=simulated_agent_id(),
        event_id=f"order_result:{outcomes[0].order_result.client_order_id}",
        received_at=now,
    )
    await db_session.commit()
    return broker, result.trade_intent_id


async def _seed_atr_bars(db_session: AsyncSession, refs: SeededRefs, *, as_of: datetime) -> None:
    """20 M15 bars with real range so `atr()` warms up past its 14-period
    default and produces a non-zero value."""
    bars = [
        Bar(
            symbol="XAUUSD",
            timeframe=Timeframe.M15,
            open_time=as_of - timedelta(minutes=15 * (20 - i)),
            open=Decimal("3410.00") + i,
            high=Decimal("3412.00") + i,
            low=Decimal("3408.00") + i,
            close=Decimal("3411.00") + i,
            tick_volume=100,
            real_volume=None,
            spread_points=2,
        )
        for i in range(20)
    ]
    await MarketDataRepository(db_session).upsert_bars(
        bars, instrument_id=refs.instrument_id, source="mt5", ingested_at=as_of
    )
    await db_session.commit()


def _loop(
    *, session_factory: async_sessionmaker[AsyncSession], redis: Redis
) -> PositionMonitorLoop:
    return PositionMonitorLoop(
        session_factory=session_factory,
        redis=redis,
        config=PositionMonitorConfig(
            trade_construction=TradeConstructionConfig(),
            atr_period=14,
            primary_tf=Timeframe.M15,
            quote_stale_seconds=5,
        ),
    )


async def test_list_open_for_management_finds_a_real_intent_backed_position(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    _, trade_intent_id = await _open_managed_position(db_session, refs)

    managed = await PositionRepository(db_session).list_open_for_management(refs.account_id)

    assert len(managed) == 1
    position = managed[0]
    assert position.symbol == "XAUUSD"
    assert position.direction == Direction.LONG
    assert position.initial_stop == _STOP
    assert position.current_stop == _STOP
    assert position.breakeven_moved is False
    assert position.partials_taken == 0

    intent_details = await TradeIntentRepository(db_session).get_order_details(trade_intent_id)
    assert intent_details.stop_loss == _STOP  # sanity: same value the position row now carries


async def test_run_for_account_is_a_noop_with_no_cached_quote(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, _ = await _open_managed_position(db_session, refs)

    loop = _loop(
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False), redis=redis_client
    )
    actioned = await loop.run_for_account(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        broker=AsyncSimulatedBrokerAdapter(broker),
        as_of=datetime.now(UTC),
    )

    assert actioned == 0
    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert row.breakeven_moved is False  # untouched - no quote meant no decision was made


async def test_run_for_account_is_a_noop_with_a_stale_cached_quote(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, _ = await _open_managed_position(db_session, refs)
    now = datetime.now(UTC)
    await _seed_atr_bars(db_session, refs, as_of=now)

    await store_quote(
        redis_client,
        account_id=refs.account_id,
        symbol="XAUUSD",
        bid=Decimal("3426.00"),  # comfortably past the breakeven threshold if it were used
        ask=Decimal("3426.25"),
        server_time=now - timedelta(seconds=30),
        received_at=now - timedelta(seconds=30),  # older than quote_stale_seconds=5
    )

    loop = _loop(
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False), redis=redis_client
    )
    actioned = await loop.run_for_account(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        broker=AsyncSimulatedBrokerAdapter(broker),
        as_of=now,
    )

    assert actioned == 0


async def test_run_for_account_moves_the_stop_to_breakeven_on_a_favorable_move(
    db_session: AsyncSession, db_engine: AsyncEngine, redis_client: Redis
) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, _ = await _open_managed_position(db_session, refs)
    now = datetime.now(UTC)
    await _seed_atr_bars(db_session, refs, as_of=now)

    # TradeConstructionConfig defaults: breakeven_at_r = 1.0, so price must
    # move by >= initial_risk_price (5.65) from entry (3418.20), i.e. past
    # 3423.85 - 3426.00 clears that without also reaching `seed_signal`'s
    # own take-profit level (3428.10), which would fire a partial close
    # instead and never reach the breakeven check.
    await store_quote(
        redis_client,
        account_id=refs.account_id,
        symbol="XAUUSD",
        bid=Decimal("3426.00"),
        ask=Decimal("3426.25"),
        server_time=now,
        received_at=now,
    )

    loop = _loop(
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False), redis=redis_client
    )
    actioned = await loop.run_for_account(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        broker=AsyncSimulatedBrokerAdapter(broker),
        as_of=now,
    )

    assert actioned == 1
    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert row.breakeven_moved is True
    assert row.stop_loss > _STOP  # moved up, toward entry - a real risk reduction
