"""SPEC-01 §2: MarketState.

The COMPLETE input to a strategy evaluation. Nothing else may be read.
Constructing MarketState is the job of the market data engine. The
backtester constructs the identical object from historical data. That
equivalence is the whole design (SPEC-07 §1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.domain.execution.intent import Position
from app.domain.market.bar import Bar
from app.domain.market.calendar_event import CalendarEvent
from app.domain.market.enums import Session, Timeframe
from app.domain.market.quote import Quote
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.portfolio.account_state import AccountState
from app.domain.risk.state import RiskState
from app.domain.strategy.signal_ref import SignalRef


@dataclass(frozen=True, slots=True)
class MarketState:
    symbol: str
    spec: SymbolSpec
    as_of: datetime  # the close time of the primary bar
    primary_tf: Timeframe
    bars: Mapping[Timeframe, tuple[Bar, ...]]  # oldest first, closed bars only
    quote: Quote
    session: Session
    account: AccountState
    open_positions: tuple[Position, ...]
    recent_signals: tuple[SignalRef, ...]  # for duplicate-setup detection
    calendar_events: tuple[CalendarEvent, ...]  # within the blackout window
    risk_state: RiskState

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("MarketState.as_of must be timezone-aware (UTC)")
        for tf, bars in self.bars.items():
            for bar in bars:
                if bar.close_time > self.as_of:
                    raise ValueError(
                        f"lookahead violation: {tf} bar closing at {bar.close_time} "
                        f"is not yet closed as of {self.as_of}"
                    )
