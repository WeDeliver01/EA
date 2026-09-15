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

`quote` is optional: no live quote cache exists yet (`event.quote` is
still logged and dropped, per `app/transport/event_router.py`), so by
default this derives one from the primary timeframe's just-closed bar -
`bid = close`, `ask = close + spread_points * point` (MT5's own bars are
bid-based, spread reported separately) - the same "quote from the last
closed bar" approach `app/research/backtester.py`'s `_build_state()`
already uses, just with real reported spread instead of a cost-model
estimate. A caller that does have a fresher live quote (once that cache
exists) can pass one in directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
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
    as_of: datetime,
    quote: Quote | None = None,
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

    if quote is None:
        primary_bars = bars.get(primary_tf, ())
        if not primary_bars:
            raise ValueError(
                f"no {primary_tf.value} bars available to derive a quote for {symbol} "
                f"as of {as_of} - pass one explicitly if this is expected"
            )
        latest = primary_bars[-1]
        spread = Decimal(latest.spread_points or 0) * spec.point
        quote = Quote(
            symbol=symbol,
            bid=latest.close,
            ask=latest.close + spread,
            server_time=latest.close_time,
            received_at=as_of,
        )

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
