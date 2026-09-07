"""SPEC-06 §6 / §10 acceptance for `Reconciler` - the DB/broker-touching
orchestration on top of `classify()`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
from app.execution.intent_service import submit_decision
from app.execution.reconciliation import PendingIntent, Reconciler
from app.models.tables import Account, PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository
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


def _reconciler(db_session: AsyncSession, broker: SimulatedBroker) -> Reconciler:
    return Reconciler(
        session=db_session,
        broker=broker,
        reconciliation_repo=ReconciliationRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        position_repo=PositionRepository(db_session),
        event_consumer=EventConsumer(
            agent_event_repo=AgentEventRepository(db_session),
            deal_repo=DealRepository(db_session),
            position_repo=PositionRepository(db_session),
            intent_repo=TradeIntentRepository(db_session),
        ),
        account_repo=AccountRepository(db_session),
    )


async def _open_position_unknown_to_local(
    db_session: AsyncSession, refs: SeededRefs
) -> tuple[SimulatedBroker, uuid.UUID, str]:
    """Fills an order via a SILENT fault: the position exists at the broker,
    but the intent is stuck UNKNOWN and no local PositionRow exists yet -
    exactly SPEC-06 §10 row 1's aftermath, which only reconciliation can
    resolve."""
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()
    signal_id = await seed_signal(db_session, refs, reference=f"SIG-REC-{uuid.uuid4().hex[:8]}")
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
    assert result.client_order_id is not None

    broker = SimulatedBroker(spec=_SPEC)
    broker.inject_fault(result.client_order_id, BrokerFault.SILENT)
    dispatcher = OutboxDispatcher(
        broker=broker,
        account_repo=AccountRepository(db_session),
        outbox_repo=OutboxRepository(db_session),
        intent_repo=TradeIntentRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )
    await dispatcher.dispatch_pending(account_id=refs.account_id, as_of=now)
    await db_session.commit()
    assert (
        await TradeIntentRepository(db_session).get_state(result.trade_intent_id)
        == ExecutionState.UNKNOWN
    )

    broker_position_id = broker.get_positions()[0].broker_position_id
    return broker, result.trade_intent_id, broker_position_id


async def test_unknown_at_broker_matched_to_intent_resolves_to_position_open(
    db_session: AsyncSession,
) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    magic = broker.get_positions()[0].magic

    pending_intent = PendingIntent(
        id=trade_intent_id,
        client_order_id="",
        magic=magic,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.10"),
        created_at=datetime.now(UTC),
        state=ExecutionState.UNKNOWN,
    )

    reconciler = _reconciler(db_session, broker)
    findings = await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=[],
        pending_intents=[pending_intent],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    assert any(f.kind == "UNKNOWN_AT_BROKER" for f in findings)
    assert (
        await TradeIntentRepository(db_session).get_state(trade_intent_id)
        == ExecutionState.POSITION_OPEN
    )
    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.broker_position_id == broker_position_id)
        )
    ).scalar_one()
    assert row.status == "OPEN"

    reconciliation_repo = ReconciliationRepository(db_session)
    assert await reconciliation_repo.has_unresolved_critical(refs.account_id) is False


async def test_unknown_at_broker_with_no_match_creates_orphaned_position(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 8: a human opens an unrelated position."""
    refs = await seed_minimal_refs(db_session)
    await _enable_trading(db_session, refs.account_id)
    await db_session.commit()

    broker = SimulatedBroker(spec=_SPEC)
    broker.simulate_manual_open(
        side=OrderSide.BUY, volume=Decimal("0.20"), price=Decimal("3405"), at=datetime.now(UTC)
    )

    reconciler = _reconciler(db_session, broker)
    findings = await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=[],
        pending_intents=[],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    assert any(f.kind == "UNKNOWN_AT_BROKER" and f.matched_intent_id is None for f in findings)
    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.account_id == refs.account_id)
        )
    ).scalar_one()
    assert row.status == "ORPHANED"
    # Never auto-closed.
    assert broker.get_positions() != ()

    reconciliation_repo = ReconciliationRepository(db_session)
    assert await reconciliation_repo.has_unresolved_critical(refs.account_id) is True


async def test_missing_at_broker_with_matching_exit_deal_closes_locally(
    db_session: AsyncSession,
) -> None:
    """SPEC-06 §10 row 7: a human closes the position manually in MT5."""
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    magic = broker.get_positions()[0].magic
    pending_intent = PendingIntent(
        id=trade_intent_id,
        client_order_id="",
        magic=magic,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.10"),
        created_at=datetime.now(UTC),
        state=ExecutionState.UNKNOWN,
    )
    reconciler = _reconciler(db_session, broker)
    await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=[],
        pending_intents=[pending_intent],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    # Now the position is known locally. A human closes it manually.
    broker.simulate_manual_close(broker_position_id, price=Decimal("3415"), at=datetime.now(UTC))

    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.broker_position_id == broker_position_id)
        )
    ).scalar_one()
    from app.execution.reconciliation import LocalOpenPosition

    local_view = [
        LocalOpenPosition(
            id=row.id,
            broker_position_id=row.broker_position_id,
            volume=row.volume,
            stop_loss=row.stop_loss,
            take_profit=row.take_profit,
            entry_price=row.entry_price,
        )
    ]
    findings = await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=local_view,
        pending_intents=[],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    assert any(f.kind == "MISSING_AT_BROKER" for f in findings)
    updated_row = await db_session.get(PositionRow, row.id)
    assert updated_row is not None
    assert updated_row.status == "CLOSED"

    reconciliation_repo = ReconciliationRepository(db_session)
    assert await reconciliation_repo.has_unresolved_critical(refs.account_id) is False


async def test_sl_mismatch_adopts_the_brokers_tighter_stop(db_session: AsyncSession) -> None:
    """SPEC-06 §10 row 9: the broker moves the stop loss."""
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    magic = broker.get_positions()[0].magic
    pending_intent = PendingIntent(
        id=trade_intent_id,
        client_order_id="",
        magic=magic,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.10"),
        created_at=datetime.now(UTC),
        state=ExecutionState.UNKNOWN,
    )
    reconciler = _reconciler(db_session, broker)
    await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=[],
        pending_intents=[pending_intent],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    broker.simulate_broker_moves_stop(broker_position_id, new_stop=Decimal("3395"))

    row = await db_session.execute(
        select(PositionRow).where(PositionRow.broker_position_id == broker_position_id)
    )
    position_row = row.scalar_one()
    assert position_row.stop_loss == Decimal("3390.00")  # our original stop, before reconciling

    from app.execution.reconciliation import LocalOpenPosition

    local_view = [
        LocalOpenPosition(
            id=position_row.id,
            broker_position_id=position_row.broker_position_id,
            volume=position_row.volume,
            stop_loss=position_row.stop_loss,
            take_profit=position_row.take_profit,
            entry_price=position_row.entry_price,
        )
    ]
    findings = await reconciler.run(
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        local_positions=local_view,
        pending_intents=[],
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    assert any(f.kind == "SL_MISMATCH" for f in findings)
    updated_row = await db_session.get(PositionRow, position_row.id)
    assert updated_row is not None
    assert updated_row.stop_loss == Decimal("3395")
