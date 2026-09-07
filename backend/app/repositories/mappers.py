"""ORM row <-> domain object conversions.

Nothing outside `app.repositories` should import `app.models` directly
(SPEC-10 Phase 1: "Repositories return domain objects, never ORM
instances").
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.execution.enums import DealType, OrderSide, PositionStatus
from app.domain.execution.intent import Fill, Position
from app.domain.market.enums import AssetClass, Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.portfolio.account_state import AccountState
from app.domain.risk.limits import RiskLimits
from app.models.tables import Account, Deal, Instrument, PositionRow, RiskProfile


def position_row_to_domain(row: PositionRow, *, symbol: str) -> Position:
    return Position(
        id=row.id,
        broker_position_id=row.broker_position_id,
        account_id=row.account_id,
        symbol=symbol,
        direction=Direction(row.direction),
        volume=row.volume,
        entry_price=row.entry_price,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        opened_at=row.opened_at,
        status=PositionStatus(row.status),
        signal_id=row.signal_id,
        initial_risk=row.initial_risk if row.initial_risk is not None else row.entry_price * 0,
        realised_pnl=row.realised_pnl,
        unrealised_pnl=row.unrealised_pnl,
        breakeven_moved=row.breakeven_moved,
        partials_taken=row.partials_taken,
    )


def account_row_to_domain(row: Account, *, is_stale: bool) -> AccountState:
    return AccountState(
        account_id=row.id,
        broker=str(row.broker_id),
        login=row.mt5_login,
        currency=row.currency,
        balance=row.balance,
        equity=row.equity,
        margin=row.margin,
        free_margin=row.free_margin,
        margin_level=row.margin_level,
        leverage=row.leverage,
        server_time=row.server_time or row.updated_at,
        reported_at=row.state_reported_at or row.updated_at,
        is_stale=is_stale,
    )


def instrument_row_to_spec(row: Instrument) -> SymbolSpec:
    return SymbolSpec(
        symbol=row.canonical_symbol,
        asset_class=AssetClass(row.asset_class),
        digits=row.digits,
        point=row.point,
        tick_size=row.tick_size,
        tick_value=row.tick_value,
        contract_size=row.contract_size,
        volume_min=row.volume_min,
        volume_max=row.volume_max,
        volume_step=row.volume_step,
        stops_level_points=row.stops_level_points,
        freeze_level_points=row.freeze_level_points,
        margin_initial=row.margin_initial if row.margin_initial is not None else Decimal(0),
        currency_profit=row.currency_profit,
        currency_margin=row.currency_margin,
        quote_currency=row.quote_currency,
    )


def risk_profile_row_to_limits(row: RiskProfile) -> RiskLimits:
    return RiskLimits(
        risk_per_trade_pct=row.risk_per_trade_pct,
        max_daily_loss_pct=row.max_daily_loss_pct,
        max_weekly_loss_pct=row.max_weekly_loss_pct,
        max_open_risk_pct=row.max_open_risk_pct,
        max_daily_trades=row.max_daily_trades,
        max_open_positions=row.max_open_positions,
        max_positions_per_symbol=row.max_positions_per_symbol,
        max_correlated_positions=row.max_correlated_positions,
        min_rr=row.min_rr,
        max_spread_multiple_of_atr=row.max_spread_multiple_of_atr,
        pause_after_consecutive_losses=row.pause_after_consecutive_losses,
        pause_duration_minutes=row.pause_duration_minutes,
        max_lot_size=row.max_lot_size,
    )


def deal_row_to_fill(row: Deal, *, symbol: str) -> Fill:
    return Fill(
        broker_deal_id=row.broker_deal_id,
        client_order_id=row.trade_intent.client_order_id if row.trade_intent is not None else "",
        broker_order_id=row.broker_order_id or "",
        broker_position_id=row.broker_position_id,
        symbol=symbol,
        side=OrderSide(row.side),
        volume=row.volume,
        price=row.price,
        commission=row.commission,
        swap=row.swap,
        profit=row.profit,
        executed_at=row.executed_at,
        deal_type=DealType(row.deal_type),
    )
