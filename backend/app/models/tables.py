"""SQLAlchemy ORM models mirroring the schema in
migrations/versions/0001_initial_schema.py (SPEC-02).

These are impure, I/O-facing types. Repositories translate between these and
the pure domain dataclasses in app.domain; nothing outside app.repositories
and app.models should touch these classes directly (SPEC-10 Phase 1
acceptance: "Repositories return domain objects, never ORM instances").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

_UUID = PGUUID(as_uuid=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="operator")
    totp_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    server: Mapped[str] = mapped_column(String)
    timezone_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("users.id"))
    broker_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("brokers.id"))
    mt5_login: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    environment: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String(3))
    leverage: Mapped[int] = mapped_column(Integer)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    margin: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    free_margin: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    margin_level: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    server_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("broker_id", "mt5_login"),)


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    broker_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("brokers.id"))
    symbol: Mapped[str] = mapped_column(String)
    canonical_symbol: Mapped[str] = mapped_column(String)
    asset_class: Mapped[str] = mapped_column(String)
    digits: Mapped[int] = mapped_column(Integer)
    point: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    tick_size: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    tick_value: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    contract_size: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    volume_min: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    volume_max: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    volume_step: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    stops_level_points: Mapped[int] = mapped_column(Integer, default=0)
    freeze_level_points: Mapped[int] = mapped_column(Integer, default=0)
    margin_initial: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    currency_profit: Mapped[str] = mapped_column(String(3))
    currency_margin: Mapped[str] = mapped_column(String(3))
    quote_currency: Mapped[str] = mapped_column(String(3))
    trading_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    spec_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("broker_id", "symbol"),)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    semver: Mapped[str] = mapped_column(String)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String)
    engine_git_sha: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("name", "semver", "config_sha256"),)


class StrategyAssignment(Base):
    __tablename__ = "strategy_assignments"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("strategy_versions.id")
    )
    primary_timeframe: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskProfile(Base):
    __tablename__ = "risk_profiles"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    version: Mapped[int] = mapped_column(Integer)
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    max_weekly_loss_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    max_open_risk_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    max_daily_trades: Mapped[int] = mapped_column(Integer)
    max_open_positions: Mapped[int] = mapped_column(Integer)
    max_positions_per_symbol: Mapped[int] = mapped_column(Integer, default=1)
    max_correlated_positions: Mapped[int] = mapped_column(Integer, default=1)
    min_rr: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    max_spread_multiple_of_atr: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    pause_after_consecutive_losses: Mapped[int] = mapped_column(Integer)
    pause_duration_minutes: Mapped[int] = mapped_column(Integer)
    max_lot_size: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    news_blackout_before_min: Mapped[int] = mapped_column(Integer, default=15)
    news_blackout_after_min: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("account_id", "version"),)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("strategy_versions.id")
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("research_datasets.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("backtest_runs.id"), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_out_of_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    risk_profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cost_model: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fill_model: Mapped[str] = mapped_column(String)
    engine_git_sha: Mapped[str] = mapped_column(String)
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("strategy_versions.id")
    )
    timeframe: Mapped[str] = mapped_column(String)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String)
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("backtest_runs.id"), nullable=True
    )
    regime: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    confluence_score: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    confluence_band: Mapped[str] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    entry: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    narrative: Mapped[str] = mapped_column(String, default="")
    engine_duration_ms: Mapped[int] = mapped_column(Integer)
    market_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    evidence: Mapped[list[AnalysisEvidence]] = relationship(back_populates="analysis_run")
    gates: Mapped[list[AnalysisGate]] = relationship(back_populates="analysis_run")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "instrument_id", "timeframe", "as_of", "strategy_version_id", "mode"
        ),
    )


class AnalysisEvidence(Base):
    __tablename__ = "analysis_evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    present: Mapped[bool] = mapped_column(Boolean)
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    score: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="evidence")


class AnalysisGate(Base):
    __tablename__ = "analysis_gates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String)
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="gates")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    reference: Mapped[str] = mapped_column(String, unique=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("analysis_runs.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("strategy_versions.id")
    )
    direction: Mapped[str] = mapped_column(String)
    setup_kind: Mapped[str] = mapped_column(String)
    setup_fingerprint: Mapped[str] = mapped_column(String)
    entry: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    take_profits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    confluence_score: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradeIntent(Base):
    __tablename__ = "trade_intents"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String, unique=True)
    signal_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("signals.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    risk_profile_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("risk_profiles.id"))
    state: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    max_slippage_points: Mapped[int] = mapped_column(Integer)
    magic: Mapped[int] = mapped_column(BigInteger)
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    risk_pct_actual: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    sizing_calculation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    risk_state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transitions: Mapped[list[ExecutionTransition]] = relationship(back_populates="trade_intent")


class ExecutionTransition(Base):
    __tablename__ = "execution_transitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_intent_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("trade_intents.id"))
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(_UUID, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    trade_intent: Mapped[TradeIntent] = relationship(back_populates="transitions")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    trade_intent_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("trade_intents.id"))
    client_order_id: Mapped[str] = mapped_column(String)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    purpose: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retcode_text: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Deal(Base):
    """Append-only (enforced by DB RULEs). The financial source of truth."""

    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    broker_deal_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("orders.id"), nullable=True
    )
    trade_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("trade_intents.id"), nullable=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    broker_position_id: Mapped[str] = mapped_column(String)
    deal_type: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    commission: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    swap: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    profit: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comment: Mapped[str | None] = mapped_column(String, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    trade_intent: Mapped[TradeIntent | None] = relationship()

    __table_args__ = (UniqueConstraint("account_id", "broker_deal_id"),)


class PositionRow(Base):
    """A projection, rebuildable from `deals` alone (see repositories.projections)."""

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    broker_position_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    trade_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("trade_intents.id"), nullable=True
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("signals.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    initial_volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    initial_stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    initial_risk: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    realised_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    unrealised_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    breakeven_moved: Mapped[bool] = mapped_column(Boolean, default=False)
    partials_taken: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    instrument: Mapped[Instrument | None] = relationship()

    __table_args__ = (UniqueConstraint("account_id", "broker_position_id"),)


class PositionEvent(Base):
    __tablename__ = "position_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("positions.id"))
    event_type: Mapped[str] = mapped_column(String)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Trade(Base):
    """A projection, rebuildable from `deals` alone (see repositories.projections)."""

    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    position_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("positions.id"), unique=True)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("signals.id"), nullable=True
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("strategy_versions.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    holding_seconds: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    commission: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    swap: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    r_multiple: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    mae: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    mfe: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    mae_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    mfe_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    entry_slippage_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_slippage_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_spread_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session: Mapped[str | None] = mapped_column(String, nullable=True)
    regime: Mapped[str | None] = mapped_column(String, nullable=True)
    confluence_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    exit_reason: Mapped[str] = mapped_column(String)
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("backtest_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(String)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    command_type: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("command_type", "idempotency_key"),)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    event_id: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    process_error: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("agent_id", "event_id"),)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String)
    transport: Mapped[str] = mapped_column(String)
    api_key_hash: Mapped[str] = mapped_column(String)
    hmac_secret_enc: Mapped[bytes] = mapped_column()
    build_version: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("account_id", "name"),)


class AgentHeartbeat(Base):
    __tablename__ = "agent_heartbeats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("agents.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    agent_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    broker_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    round_trip_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_connected: Mapped[bool] = mapped_column(Boolean)
    trade_allowed: Mapped[bool] = mapped_column(Boolean)
    balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    open_position_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String)
    local_position_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broker_position_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discrepancy_count: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Discrepancy(Base):
    __tablename__ = "discrepancies"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reconciliation_runs.id")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    kind: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    local_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    broker_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("accounts.id"))
    type: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(_UUID, nullable=True)
    message: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(_UUID, ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String)
    target_type: Mapped[str | None] = mapped_column(String, nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(_UUID, nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("refresh_tokens.id"), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class CalendarEventRow(Base):
    __tablename__ = "calendar_events"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3))
    impact: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    actual: Mapped[str | None] = mapped_column(String, nullable=True)
    forecast: Mapped[str | None] = mapped_column(String, nullable=True)
    previous: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("event_time", "currency", "title"),)


class ResearchDataset(Base):
    __tablename__ = "research_datasets"

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("brokers.id"), nullable=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(_UUID, ForeignKey("instruments.id"))
    kind: Mapped[str] = mapped_column(String)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String)
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    oos_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BacktestMetrics(Base):
    __tablename__ = "backtest_metrics"

    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True
    )
    trade_count: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    expectancy_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    avg_win_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    avg_loss_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    net_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    cagr: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    max_drawdown_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    sortino: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    calmar: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    longest_loss_streak: Mapped[int | None] = mapped_column(Integer, nullable=True)
    longest_win_streak: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_in_market_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    trades_per_year: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    profit_concentration_top1_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    profit_concentration_top5_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    best_year_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    worst_year_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    profitable_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    longest_no_new_high_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yearly_returns: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    regime_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    session_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    gate_rejection_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EquityCurvePoint(Base):
    __tablename__ = "equity_curve_points"

    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    open_positions: Mapped[int] = mapped_column(Integer)


class MarketBar(Base):
    __tablename__ = "market_bars"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("instruments.id"), primary_key=True
    )
    timeframe: Mapped[str] = mapped_column(String, primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    tick_volume: Mapped[int] = mapped_column(BigInteger)
    real_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    spread_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketTick(Base):
    __tablename__ = "market_ticks"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("instruments.id"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    bid: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    ask: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    flags: Mapped[int | None] = mapped_column(Integer, nullable=True)
