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
from app.execution.broker import AsyncSimulatedBrokerAdapter, BrokerFault, SimulatedBroker
from app.execution.dispatcher import OutboxDispatcher
from app.execution.event_consumer import EventConsumer
from app.execution.intent_service import submit_decision
from app.execution.reconciliation import Finding, PendingIntent, Reconciler
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
        broker=AsyncSimulatedBrokerAdapter(broker),
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
        broker=AsyncSimulatedBrokerAdapter(broker),
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


async def _attach_locally(
    db_session: AsyncSession,
    refs: SeededRefs,
    *,
    broker: SimulatedBroker,
    trade_intent_id: uuid.UUID,
    broker_position_id: str,
) -> PositionRow:
    """Runs one reconciliation pass to turn the broker-only position from
    `_open_position_unknown_to_local` into a real local `PositionRow`, for
    tests that need a genuine local position to attach a hand-built
    `Finding` to."""
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
    row = (
        await db_session.execute(
            select(PositionRow).where(PositionRow.broker_position_id == broker_position_id)
        )
    ).scalar_one()
    return row


async def test_unhandled_discrepancy_kinds_are_recorded_but_never_auto_resolved(
    db_session: AsyncSession,
) -> None:
    """VOLUME_MISMATCH, TP_MISMATCH and PRICE_MISMATCH have no dedicated
    resolver (see the ADR) - `_apply_resolution`'s fallthrough must still
    record them, just without touching anything."""
    refs = await seed_minimal_refs(db_session)
    reconciler = _reconciler(db_session, SimulatedBroker(spec=_SPEC))
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="VOLUME_MISMATCH",
        severity="warning",
        local_state={"volume": "0.10"},
        broker_state={"volume": "0.20"},
    )
    await reconciler._apply_resolution(
        finding,
        run_id=run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert any(d.kind == "VOLUME_MISMATCH" for d in unresolved)


async def test_missing_at_broker_with_no_matching_deal_stays_unresolved(
    db_session: AsyncSession,
) -> None:
    """No deal evidence yet of how the position closed - stays unresolved
    rather than guessing."""
    refs = await seed_minimal_refs(db_session)
    reconciler = _reconciler(db_session, SimulatedBroker(spec=_SPEC))
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="MISSING_AT_BROKER",
        severity="critical",
        local_state={"volume": "0.10"},
        broker_state=None,
        broker_position_id="no-such-position",
    )
    await reconciler._resolve_missing_at_broker(
        finding,
        run_id=run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert any(d.kind == "MISSING_AT_BROKER" for d in unresolved)


async def test_missing_at_broker_with_zero_profit_deal_skips_pnl_update(
    db_session: AsyncSession,
) -> None:
    """A flat close (exit price == entry price) is zero-profit and legitimate
    - the account balance must not move for a no-op P&L."""
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    await _attach_locally(
        db_session,
        refs,
        broker=broker,
        trade_intent_id=trade_intent_id,
        broker_position_id=broker_position_id,
    )
    account_before = await AccountRepository(db_session).load_account_state(
        refs.account_id, as_of=datetime.now(UTC)
    )

    # Flat manual close: same price as entry (3400.00), so profit is 0.
    broker.simulate_manual_close(broker_position_id, price=Decimal("3400.00"), at=datetime.now(UTC))

    reconciler = _reconciler(db_session, broker)
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="MISSING_AT_BROKER",
        severity="critical",
        local_state={"volume": "0.10"},
        broker_state=None,
        broker_position_id=broker_position_id,
    )
    await reconciler._resolve_missing_at_broker(
        finding,
        run_id=run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        as_of=datetime.now(UTC),
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert not any(d.kind == "MISSING_AT_BROKER" for d in unresolved)  # resolved
    account_after = await AccountRepository(db_session).load_account_state(
        refs.account_id, as_of=datetime.now(UTC)
    )
    assert account_after.balance == account_before.balance


async def test_sl_mismatch_sets_the_brokers_missing_stop(db_session: AsyncSession) -> None:
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    local_row = await _attach_locally(
        db_session,
        refs,
        broker=broker,
        trade_intent_id=trade_intent_id,
        broker_position_id=broker_position_id,
    )

    reconciler = _reconciler(db_session, broker)
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="SL_MISMATCH",
        severity="warning",
        local_state={"stop_loss": "3390.00"},
        broker_state={"stop_loss": None},
        local_position_id=local_row.id,
        broker_position_id=broker_position_id,
    )
    await reconciler._resolve_sl_mismatch(
        finding, run_id=run_id, account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert not any(d.kind == "SL_MISMATCH" for d in unresolved)  # resolved
    assert broker.get_positions()[0].stop_loss == Decimal("3390.00")


async def test_sl_mismatch_with_no_local_stop_stays_unresolved(db_session: AsyncSession) -> None:
    """Neither branch handles "local has no stop at all" - documented gap,
    pinned here rather than left silently uncovered."""
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )
    local_row = await _attach_locally(
        db_session,
        refs,
        broker=broker,
        trade_intent_id=trade_intent_id,
        broker_position_id=broker_position_id,
    )

    reconciler = _reconciler(db_session, broker)
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="SL_MISMATCH",
        severity="warning",
        local_state={"stop_loss": None},
        broker_state={"stop_loss": "3395.00"},
        local_position_id=local_row.id,
        broker_position_id=broker_position_id,
    )
    await reconciler._resolve_sl_mismatch(
        finding, run_id=run_id, account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert any(d.kind == "SL_MISMATCH" for d in unresolved)


async def test_sl_mismatch_adopts_broker_stop_even_if_the_local_row_is_gone(
    db_session: AsyncSession,
) -> None:
    """The discrepancy still resolves (the broker's value is authoritative
    either way) even if the local position row can't be found to update."""
    refs = await seed_minimal_refs(db_session)
    broker, trade_intent_id, broker_position_id = await _open_position_unknown_to_local(
        db_session, refs
    )

    reconciler = _reconciler(db_session, broker)
    run_id = await ReconciliationRepository(db_session).start_run(
        refs.account_id, started_at=datetime.now(UTC)
    )
    finding = Finding(
        kind="SL_MISMATCH",
        severity="warning",
        local_state={"stop_loss": "3390.00"},
        broker_state={"stop_loss": "3395.00"},
        local_position_id=uuid.uuid4(),  # no such row
        broker_position_id=broker_position_id,
    )
    await reconciler._resolve_sl_mismatch(
        finding, run_id=run_id, account_id=refs.account_id, as_of=datetime.now(UTC)
    )
    await db_session.commit()

    unresolved = await ReconciliationRepository(db_session).list_unresolved(refs.account_id)
    assert not any(d.kind == "SL_MISMATCH" for d in unresolved)  # resolved
