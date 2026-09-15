"""Builds a live `MarketState` for `StrategyEngine.evaluate()` - the real
equivalent of what `app/research/backtester.py`'s private `_build_state()`
does from an in-memory `dict`, reading instead from the database (bars,
account/risk state, open positions, recent signals).

Deliberately not in `app/engines/`: engines import only domain (no
`sqlalchemy`, no repositories - `pyproject.toml`'s import-linter contract
enforces this), and this function's entire job is I/O. It lives in
`app/workers/` because that's the layer both `market_scanner` and
`strategy_worker` already sit in, and it exists only to be called by them.

`calendar_events` is always empty here, matching `app/research/backtester.py`'s
own documented gap (see the ADR): no calendar/news ingestion exists
anywhere in this codebase yet, live or backtested.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from app.domain.market.enums import Timeframe
from app.domain.market.market_state import MarketState
from app.domain.market.quote import Quote
from app.engines.context.sessions import classify_session
from app.repositories.accounts import AccountRepository
from app.repositories.market_data import MarketDataRepository
from app.repositories.positions import PositionRepository
from app.repositories.signals import SignalRepository

_DEFAULT_LOOKBACK_BARS = 300


async def build_market_state(
    *,
    account_repo: AccountRepository,
    market_data_repo: MarketDataRepository,
    position_repo: PositionRepository,
    signal_repo: SignalRepository,
    account_id: UUID,
    instrument_id: UUID,
    symbol: str,
    primary_tf: Timeframe,
    context_timeframes: Sequence[Timeframe],
    quote: Quote,
    as_of: datetime,
    signal_lookback: timedelta = timedelta(days=1),
    lookback_bars: int = _DEFAULT_LOOKBACK_BARS,
) -> MarketState:
    spec = await account_repo.load_symbol_spec(instrument_id)
    account = await account_repo.load_account_state(account_id, as_of=as_of)
    risk_state = await account_repo.compute_risk_state(account_id, as_of=as_of)
    open_positions = await position_repo.list_open(account_id)

    bars = {
        tf: await market_data_repo.get_recent_closed_bars(
            instrument_id, tf, symbol=symbol, before=as_of, limit=lookback_bars
        )
        for tf in {primary_tf, *context_timeframes}
    }

    recent_signals = await signal_repo.list_recent(
        account_id, instrument_id, symbol=symbol, since=as_of - signal_lookback
    )

    return MarketState(
        symbol=symbol,
        spec=spec,
        as_of=as_of,
        primary_tf=primary_tf,
        bars=bars,
        quote=quote,
        session=classify_session(as_of),
        account=account,
        open_positions=open_positions,
        recent_signals=recent_signals,
        calendar_events=(),
        risk_state=risk_state,
    )
