"""SPEC-01 §2: Bar.

Bar convention, stated once and never violated: ``open_time`` is the bar's
opening timestamp in UTC. A bar is closed and eligible for analysis only when
``now >= open_time + timeframe.seconds + CANDLE_CLOSE_GRACE_MS``. The
strategy engine never sees a forming bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.market.enums import Timeframe


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: Timeframe
    open_time: datetime  # UTC, always the bar OPEN
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int
    real_volume: int | None
    spread_points: int | None

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None:
            raise ValueError("Bar.open_time must be timezone-aware (UTC)")
        if self.low > self.high:
            raise ValueError("Bar.low cannot exceed Bar.high")
        if not (self.low <= self.open <= self.high):
            raise ValueError("Bar.open must lie within [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("Bar.close must lie within [low, high]")

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=self.timeframe.seconds)

    @property
    def body(self) -> Decimal:
        return self.close - self.open

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def body_ratio(self) -> Decimal:
        if self.range == 0:
            return Decimal(0)
        return abs(self.body) / self.range
