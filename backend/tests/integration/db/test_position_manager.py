"""SPEC-06 §8 acceptance for `PositionManager.apply()` - the DB/broker-
touching half of position management, on top of a real open position."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import OrderSide
from app.domain.market.enums import AssetClass, Direction, Regime
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.execution.broker import AsyncSimulatedBrokerAdapter, SimulatedBroker
from app.execution.dispatcher import OutboxDispatcher
from app.execution.event_consumer import EventConsumer
from app.execution.intent_service import submit_decision
from app.execution.position_manager import LivePosition, ManagementAction, PositionManager
from app.models.tables import Account, PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository, simulated_agent_id
from app.repositories.deals import DealRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.positions import PositionRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository
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


async def _open_position(
    db_session: AsyncSession, refs: SeededRefs
) -> tuple[SimulatedBroker, LivePosition]:
    account = await db_session.get(Account, refs.account_id)
    assert account is not None
    account.trading_enabled = True
    account.kill_switch_active = False
    await db_session.commit()

    signal_id = await seed_signal(db_session, refs, reference=f"SIG-PM-{uuid.uuid4().hex[:8]}")
    now = datetime.now(UTC)
    decision = Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=now,
        strategy_version_id=uuid.uuid4(),
        regime=Regime.TRENDING_UP,
        setup=None,
        direction=Direction.LONG,
        entry=Decimal("3400.00"),
        stop_loss=Decimal("3390.00"),
        take_profits=(
            TakeProfit(
                level=Decimal("3420.00"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")
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

    position_row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()

    live_position = LivePosition(
        id=position_row.id,
        broker_position_id=position_row.broker_position_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        direction=Direction.LONG,
        initial_volume=position_row.initial_volume,
        remaining_volume=position_row.volume,
        entry_price=position_row.entry_price,
        initial_stop=Decimal("3390.00"),
        current_stop=Decimal("3390.00"),
        take_profits=decision.take_profits,
        partials_taken=0,
        breakeven_moved=False,
        opened_at=position_row.opened_at,
    )
    return broker, live_position


def _manager(db_session: AsyncSession, broker: SimulatedBroker) -> PositionManager:
    return PositionManager(
        broker=AsyncSimulatedBrokerAdapter(broker),
        event_consumer=EventConsumer(
            agent_event_repo=AgentEventRepository(db_session),
            deal_repo=DealRepository(db_session),
            position_repo=PositionRepository(db_session),
            intent_repo=TradeIntentRepository(db_session),
        ),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
    )


async def test_modify_stop_updates_the_position_row(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, position = await _open_position(db_session, refs)

    manager = _manager(db_session, broker)
    action = ManagementAction(kind="modify_stop", new_stop=Decimal("3402"), sets_breakeven=True)
    await manager.apply(position, action, spec=_SPEC, at=datetime.now(UTC))
    await db_session.commit()

    row = await db_session.get(PositionRow, position.id)
    assert row is not None
    assert row.stop_loss == Decimal("3402")
    assert row.breakeven_moved is True
    assert broker.get_positions()[0].stop_loss == Decimal("3402")


async def test_close_full_realises_pnl_and_closes_the_position(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, position = await _open_position(db_session, refs)

    account_before = await AccountRepository(db_session).load_account_state(
        refs.account_id, as_of=datetime.now(UTC)
    )

    manager = _manager(db_session, broker)
    action = ManagementAction(
        kind="close_full",
        close_volume=position.remaining_volume,
        close_price=Decimal("3420.00"),
        close_reason="TIME_EXIT",
    )
    await manager.apply(position, action, spec=_SPEC, at=datetime.now(UTC))
    await db_session.commit()

    row = await db_session.get(PositionRow, position.id)
    assert row is not None
    assert row.status == "CLOSED"
    assert broker.get_positions() == ()

    account_after = await AccountRepository(db_session).load_account_state(
        refs.account_id, as_of=datetime.now(UTC)
    )
    # (3420-3400)/0.01 ticks * $1.00/tick * volume - realised as account P&L.
    expected_profit = (
        (Decimal("3420.00") - Decimal("3400.00")) / _SPEC.tick_size * _SPEC.tick_value
    ) * position.remaining_volume
    assert account_after.balance - account_before.balance == expected_profit


async def test_close_partial_leaves_the_position_open_with_reduced_volume(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, position = await _open_position(db_session, refs)

    manager = _manager(db_session, broker)
    half = position.remaining_volume / 2
    action = ManagementAction(
        kind="close_partial",
        close_volume=half,
        close_price=Decimal("3410.00"),
        close_reason="PARTIAL_TP",
        rung_index=0,
    )
    await manager.apply(position, action, spec=_SPEC, at=datetime.now(UTC))
    await db_session.commit()

    row = await db_session.get(PositionRow, position.id)
    assert row is not None
    assert row.status == "OPEN"
    assert row.volume == position.remaining_volume - half
    assert row.partials_taken == 1
