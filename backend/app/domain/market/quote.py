"""SPEC-01 §2: Quote."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    server_time: datetime  # broker time, converted to UTC
    received_at: datetime  # our clock, UTC

    def __post_init__(self) -> None:
        if self.server_time.tzinfo is None:
            raise ValueError("Quote.server_time must be timezone-aware (UTC)")
        if self.received_at.tzinfo is None:
            raise ValueError("Quote.received_at must be timezone-aware (UTC)")
        if self.ask < self.bid:
            raise ValueError("Quote.ask cannot be less than Quote.bid")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def mid(self) -> Decimal:
        return (self.ask + self.bid) / 2
