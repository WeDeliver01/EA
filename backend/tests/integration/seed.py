"""Minimal reference-data seeding for integration tests. Builds just enough
of the FK chain (users -> brokers -> accounts -> instruments ->
strategy_versions -> risk_profiles -> analysis_runs -> signals) to exercise
trade_intents / deals / positions / trades in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import (
    Account,
    AnalysisRun,
    Broker,
    Instrument,
    RiskProfile,
    Signal,
    StrategyVersion,
    User,
)


@dataclass(frozen=True, slots=True)
class SeededRefs:
    user_id: UUID
    broker_id: UUID
    account_id: UUID
    instrument_id: UUID
    strategy_version_id: UUID
    risk_profile_id: UUID
    analysis_run_id: UUID


async def seed_minimal_refs(session: AsyncSession) -> SeededRefs:
    now = datetime.now(UTC)

    user = User(
        id=uuid4(),
        email=f"{uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Test Operator",
        role="operator",
        created_at=now,
        updated_at=now,
    )
    broker = Broker(
        id=uuid4(),
        name=f"TestBroker-{uuid4().hex[:6]}",
        server="demo.testbroker.com",
        created_at=now,
    )
    session.add_all([user, broker])
    await session.flush()

    account = Account(
        id=uuid4(),
        user_id=user.id,
        broker_id=broker.id,
        mt5_login="12345678",
        label="Test Account",
        environment="demo",
        currency="USD",
        leverage=500,
        balance=Decimal("10000.00"),
        equity=Decimal("10000.00"),
        free_margin=Decimal("10000.00"),
        peak_equity=Decimal("10000.00"),
        created_at=now,
        updated_at=now,
    )
    instrument = Instrument(
        id=uuid4(),
        broker_id=broker.id,
        symbol="XAUUSD",
        canonical_symbol="XAUUSD",
        asset_class="METAL",
        digits=2,
        point=Decimal("0.01"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1.00"),
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("50"),
        volume_step=Decimal("0.01"),
        currency_profit="USD",
        currency_margin="USD",
        quote_currency="USD",
        spec_fetched_at=now,
    )
    session.add_all([account, instrument])
    await session.flush()

    strategy_version = StrategyVersion(
        id=uuid4(),
        name="tdip",
        semver="0.1.0",
        config={},
        config_sha256="0" * 64,
        engine_git_sha="test",
        label="tdip@0.1.0+test",
        created_at=now,
    )
    session.add(strategy_version)
    await session.flush()

    risk_profile = RiskProfile(
        id=uuid4(),
        account_id=account.id,
        version=1,
        risk_per_trade_pct=Decimal("0.005"),
        max_daily_loss_pct=Decimal("0.03"),
        max_weekly_loss_pct=Decimal("0.06"),
        max_open_risk_pct=Decimal("0.02"),
        max_daily_trades=5,
        min_rr=Decimal("1.5"),
        max_spread_multiple_of_atr=Decimal("0.12"),
        pause_after_consecutive_losses=3,
        pause_duration_minutes=60,
        max_lot_size=Decimal("5"),
        max_open_positions=3,
        is_active=True,
        created_at=now,
    )
    session.add(risk_profile)
    await session.flush()

    analysis_run = AnalysisRun(
        id=uuid4(),
        account_id=account.id,
        instrument_id=instrument.id,
        strategy_version_id=strategy_version.id,
        timeframe="M15",
        as_of=now,
        mode="paper",
        regime="EXPANSION",
        outcome="TRADE",
        confluence_score=Decimal("8.0"),
        confluence_band="HIGH",
        engine_duration_ms=5,
        market_snapshot={},
        created_at=now,
    )
    session.add(analysis_run)
    await session.flush()

    return SeededRefs(
        user_id=user.id,
        broker_id=broker.id,
        account_id=account.id,
        instrument_id=instrument.id,
        strategy_version_id=strategy_version.id,
        risk_profile_id=risk_profile.id,
        analysis_run_id=analysis_run.id,
    )


async def seed_signal(session: AsyncSession, refs: SeededRefs, *, reference: str) -> UUID:
    now = datetime.now(UTC)
    signal = Signal(
        id=uuid4(),
        reference=reference,
        analysis_run_id=refs.analysis_run_id,
        account_id=refs.account_id,
        instrument_id=refs.instrument_id,
        strategy_version_id=refs.strategy_version_id,
        direction="LONG",
        setup_kind="BREAKOUT",
        setup_fingerprint=uuid4().hex,
        entry=Decimal("3418.20"),
        stop_loss=Decimal("3412.55"),
        take_profits=[{"level": "3428.10", "fraction": "1.0", "r_multiple": "1.75"}],
        confluence_score=Decimal("8.0"),
        expires_at=now,
        created_at=now,
    )
    session.add(signal)
    await session.flush()
    return signal.id
