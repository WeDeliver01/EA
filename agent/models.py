"""SPEC-04 wire-format types.

The agent runs on a different machine from the backend and ships
independently (Windows-only, `MetaTrader5` package). It does not import
`backend.app`: the two processes share a JSON protocol (SPEC-04), not a
Python dependency. These types mirror the backend's domain shapes closely
enough that (de)serialising them is a no-op field-for-field mapping, but
they are defined here so the agent's dependency set stays MT5 + transport
only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class DealType(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    PARTIAL_EXIT = "PARTIAL_EXIT"


@dataclass(frozen=True, slots=True)
class PlaceOrderCommand:
    """`command.place_order` payload, SPEC-04 §4."""

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    limit_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    max_slippage_points: int
    magic: int
    comment: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """`event.order_result` payload, SPEC-04 §5."""

    client_order_id: str
    retcode: int
    retcode_text: str
    broker_order_id: str | None
    broker_deal_id: str | None
    broker_position_id: str | None
    filled_volume: Decimal
    fill_price: Decimal | None
    requested_price: Decimal
    slippage_points: int
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModifyResult:
    broker_position_id: str
    retcode: int
    retcode_text: str


@dataclass(frozen=True, slots=True)
class Fill:
    """`event.deal` payload, SPEC-04 §5."""

    broker_deal_id: str
    client_order_id: str
    broker_order_id: str
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    executed_at: datetime
    deal_type: DealType


@dataclass(frozen=True, slots=True)
class BrokerPositionSnapshot:
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    magic: int
    comment: str


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """SPEC-01 §2 shape, as read from `mt5.symbol_info`. Never hard-coded."""

    symbol: str
    digits: int
    point: Decimal
    tick_size: Decimal
    tick_value: Decimal
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int
    freeze_level_points: int
    margin_initial: Decimal
    currency_profit: str
    currency_margin: str


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    login: int
    server: str
    currency: str
    leverage: int
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class TerminalHealth:
    connected: bool
    trade_allowed: bool
    algo_trading_enabled: bool
    build: int
    broker_time: datetime | None
