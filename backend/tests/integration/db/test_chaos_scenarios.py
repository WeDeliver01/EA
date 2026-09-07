"""SPEC-06 §10 chaos scenarios not already covered by
test_dispatcher.py / test_event_consumer.py / test_reconciliation.py, plus
the SPEC-10 Phase 4 acceptance criterion of one full lifecycle test: signal
-> intent -> fill -> management -> close -> trade record.

Rows covered elsewhere: 1 (dropped connection after place_order) and 4
(crash after dispatch, before result) in test_dispatcher.py /
test_reconciliation.py; 2 (duplicate order_result) in test_event_consumer.py;
7 (manual close) and 9 (broker moves stop) in test_reconciliation.py; 10
(two workers, one intent) in test_outbox.py; 12 (daily loss limit mid-
dispatch) in test_dispatcher.py.

Row 11 (clock skew on agent handshake) is Phase 5 territory - there is no
real agent handshake in this MVP (see docs/adr/0001-mvp-scope.md) - and is
not covered anywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import ExecutionState, OrderSide
from app.domain.market.enums import AssetClass, Direction, Regime
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.execution.broker import BrokerFault, SimulatedBroker
from app.execution.dispatcher import OutboxDispatcher
from app.execution.event_consumer import EventConsumer
from app.execution.intent_service import SubmitResult, submit_decision
from app.execution.position_manager import LivePosition, PositionManager, decide
from app.execution.reconciliation import Reconciler
from app.models.tables import Account, PositionRow, Trade
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


async def _enable_trading(session: AsyncSession, account_id: uuid.UUID) -> None:
    account = await session.get(Account, account_id)
    assert account is not None
    account.trading_enabled = True
    account.kill_switch_active = False
    await session.flush()


def _trade_decision(*, as_of: datetime) -> Decision:
    return Decision(
        outcome=DecisionOutcome.TRADE,
        symbol="XAUUSD",
        as_of=as_of,
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


def _dispatcher(db_session: AsyncSession, broker: SimulatedBroker) -> OutboxDispatcher:
    return OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )


def _consumer(db_session: AsyncSession) -> EventConsumer:
    return EventConsumer(
        agent_event_repo=AgentEventRepository(db_session),
        deal_repo=DealRepository(db_session),
        position_repo=PositionRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
    )


async def _submit(db_session: AsyncSession, refs: SeededRefs, *, reference: str) -> SubmitResult:
    signal_id = await seed_signal(db_session, refs, reference=reference)
    now = datetime.now(UTC)
    result = await submit_decision(
        _trade_decision(as_of=now),
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
    return result


async def test_crash_between_outbox_commit_and_dispatch_sends_exactly_once(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 3. A fresh `OutboxDispatcher` instance (standing in
    for a restarted process) is what actually proves this: nothing here
    keeps in-memory state between the commit and the "restart"."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-CHAOS-1")
    # The outbox row is committed here. A real crash would kill the process
    # before any dispatcher ever ran against it.

    broker = SimulatedBroker(spec=_SPEC)
    fresh_dispatcher = _dispatcher(db_session, broker)  # simulates the restarted process
    outcomes = await fresh_dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert len(outcomes) == 1
    assert outcomes[0].sent is True
    assert len(broker.get_positions()) == 1

    # A second dispatch pass (as if the "restarted" worker looped again)
    # must find nothing left to send - the row is already dispatched.
    second_pass = await fresh_dispatcher.dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    assert second_pass == []
    assert len(broker.get_positions()) == 1  # still exactly one position

    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.SENT
    )


async def test_partial_fill_opens_a_position_sized_to_actual_fill(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 6."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    result = await _submit(db_session, refs, reference="SIG-CHAOS-2")
    assert result.client_order_id is not None

    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault(result.client_order_id, BrokerFault.PARTIAL_FILL, fraction="0.5")
    outcomes = await _dispatcher(db_session, broker).dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()
    assert outcomes[0].order_result is not None
    filled_volume = outcomes[0].order_result.filled_volume

    consumer = _consumer(db_session)
    await consumer.process_order_result(
        outcomes[0].order_result,
        trade_intent_id=result.trade_intent_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        agent_id=simulated_agent_id(),
        event_id=f"order_result:{result.client_order_id}",
        received_at=datetime.now(UTC),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert row.status == "OPEN"
    assert row.volume == filled_volume
    assert filled_volume < Decimal("0.10")  # confirms the fault actually reduced the fill


async def test_unresolved_critical_discrepancy_blocks_new_entries(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 8, second half: entries blocked, nothing auto-closed."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    broker.simulate_manual_open(
        side=OrderSide.BUY, volume=Decimal("0.20"), price=Decimal("3405"), at=datetime.now(UTC)
    )
    reconciler = Reconciler(
        session=db_session,
        broker=broker,
        reconciliation_repo=ReconciliationRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        position_repo=PositionRepository(db_session),
        event_consumer=_consumer(db_session),
        account_repo=AccountRepository(db_session),
    )
    await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=[],
        pending_intents=[],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()
    assert await ReconciliationRepository(db_session).has_unresolved_critical(refs.account_id)

    # A brand new signal now tries to submit and dispatch.
    result = await _submit(db_session, refs, reference="SIG-CHAOS-3")
    outcomes = await _dispatcher(db_session, broker).dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    assert outcomes[0].sent is False
    assert outcomes[0].cancelled_reason == "RECONCILIATION_UNRESOLVED"
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.CANCELLED
    )
    # The orphaned position is still there, untouched.
    assert len(broker.get_positions()) == 1


async def test_full_lifecycle_signal_to_intent_to_fill_to_management_to_close_to_trade(
    db_session: AsyncSession,
) -> None:
    """SPEC-10 Phase 4 acceptance: 'A full lifecycle runs: signal to intent
    to fill to management to close to trade record.'"""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    # signal -> intent
    result = await _submit(db_session, refs, reference="SIG-CHAOS-4")
    assert result.state == ExecutionState.SENT

    # intent -> fill
    broker = SimulatedBroker(spec=_SPEC)
    outcomes = await _dispatcher(db_session, broker).dispatch_pending(
        account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()
    assert outcomes[0].order_result is not None

    consumer = _consumer(db_session)
    consume_result = await consumer.process_order_result(
        outcomes[0].order_result,
        trade_intent_id=result.trade_intent_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        agent_id=simulated_agent_id(),
        event_id=f"order_result:{result.client_order_id}",
        received_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert consume_result.new_state == ExecutionState.POSITION_OPEN

    position_row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert position_row.stop_loss == Decimal("3390.00")
    assert position_row.initial_risk is not None and position_row.initial_risk > 0
    assert position_row.initial_stop_loss is not None
    assert position_row.stop_loss is not None

    # management: breakeven moves once price runs favourably.
    live_position = LivePosition(
        id=position_row.id,
        broker_position_id=position_row.broker_position_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        direction=Direction.LONG,
        initial_volume=position_row.initial_volume,
        remaining_volume=position_row.volume,
        entry_price=position_row.entry_price,
        initial_stop=position_row.initial_stop_loss,
        current_stop=position_row.stop_loss,
        take_profits=(
            TakeProfit(
                level=Decimal("3420.00"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")
            ),
        ),
        partials_taken=position_row.partials_taken,
        breakeven_moved=position_row.breakeven_moved,
        opened_at=position_row.opened_at,
    )
    from app.engines.config import TradeConstructionConfig

    cfg = TradeConstructionConfig(
        breakeven_at_r=Decimal("1.0"), breakeven_buffer_atr=Decimal("0.1"), trail_mode="none"
    )
    action = decide(
        live_position,
        current_price=Decimal("3410"),  # +10, exactly the initial risk -> breakeven fires
        atr=Decimal("3"),
        cfg=cfg,
        spec=_SPEC,
        as_of=datetime.now(UTC),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "modify_stop"

    manager = PositionManager(
        broker=broker,
        event_consumer=consumer,
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
    )
    await manager.apply(live_position, action, spec=_SPEC, at=datetime.now(UTC))
    await db_session.commit()

    updated_row = await db_session.get(PositionRow, position_row.id)
    assert updated_row is not None
    assert updated_row.breakeven_moved is True
    assert updated_row.stop_loss == action.new_stop
    assert updated_row.initial_stop_loss is not None
    assert updated_row.stop_loss is not None

    # close: time exit at the take-profit level.
    live_position_after_be = LivePosition(
        id=updated_row.id,
        broker_position_id=updated_row.broker_position_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        direction=Direction.LONG,
        initial_volume=updated_row.initial_volume,
        remaining_volume=updated_row.volume,
        entry_price=updated_row.entry_price,
        initial_stop=updated_row.initial_stop_loss,
        current_stop=updated_row.stop_loss,
        take_profits=live_position.take_profits,
        partials_taken=updated_row.partials_taken,
        breakeven_moved=updated_row.breakeven_moved,
        opened_at=updated_row.opened_at,
    )
    close_action = decide(
        live_position_after_be,
        current_price=Decimal("3420"),
        atr=Decimal("3"),
        cfg=cfg,
        spec=_SPEC,
        as_of=datetime.now(UTC),
        max_holding_duration=timedelta(hours=24),
    )
    assert close_action.kind == "close_partial"  # the take-profit rung, at full volume (one rung)

    await manager.apply(live_position_after_be, close_action, spec=_SPEC, at=datetime.now(UTC))
    await db_session.commit()

    # close -> trade record.
    final_row = await db_session.get(PositionRow, position_row.id)
    assert final_row is not None
    assert final_row.status == "CLOSED"

    trade = (
        await db_session.execute(select(Trade).where(Trade.position_id == position_row.id))
    ).scalar_one()
    assert trade.net_pnl > 0
    assert trade.direction == "LONG"
