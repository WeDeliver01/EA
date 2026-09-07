"""SPEC-01 §2: SymbolSpec.

Broker contract specification. Fetched from MT5, never guessed. ``tick_value``
and ``contract_size`` are pulled from ``mt5.symbol_info()`` on every agent
connect. Position sizing that hard-codes pip values is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.market.enums import AssetClass


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    asset_class: AssetClass
    digits: int
    point: Decimal
    tick_size: Decimal
    tick_value: Decimal  # account currency value of one tick per 1.0 lot
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int  # broker minimum SL/TP distance from price
    freeze_level_points: int
    margin_initial: Decimal
    currency_profit: str
    currency_margin: str
    quote_currency: str
