"""Shared test factories for building domain objects with sensible defaults.

Used by unit, golden and integration tests alike so that a MarketState fixture
doesn't require re-deriving every nested value object by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.execution.intent import Position
from app.domain.market.bar import Bar
from app.domain.market.calendar_event import CalendarEvent, CalendarImpact
from app.domain.market.enums import AssetClass, Session, Timeframe
from app.domain.market.market_state import MarketState
from app.domain.market.quote import Quote
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.portfolio.account_state import AccountState
from app.domain.risk.state import RiskState

XAUUSD_SPEC = SymbolSpec(
    symbol="XAUUSD",
    asset_class=AssetClass.METAL,
    digits=2,
    point=Decimal("0.01"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1.00"),
    contract_size=Decimal("100"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("50"),
    volume_step=Decimal("0.01"),
    stops_level_points=50,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)


def make_bars(
    *,
    symbol: str = "XAUUSD",
    timeframe: Timeframe = Timeframe.M15,
    count: int,
    start: datetime,
    start_price: Decimal = Decimal("3400.00"),
    step: Decimal = Decimal("0.50"),
    trend: Decimal = Decimal("0"),
    high_extra: Decimal = Decimal("0.80"),
    low_extra: Decimal = Decimal("0.80"),
) -> tuple[Bar, ...]:
    """Deterministic synthetic bars: a simple oscillation plus an optional
    per-bar drift, entirely reproducible from the given seed values."""
    bars: list[Bar] = []
    price = start_price
    for i in range(count):
        open_time = start + timedelta(seconds=timeframe.seconds * i)
        direction = Decimal(1) if i % 2 == 0 else Decimal(-1)
        open_ = price
        close = price + direction * step + trend
        high = max(open_, close) + high_extra
        low = min(open_, close) - low_extra
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=100 + i,
                real_volume=None,
                spread_points=20,
            )
        )
        price = close
    return tuple(bars)


def make_account_state(
    *,
    account_id: UUID | None = None,
    balance: Decimal = Decimal("10000.00"),
    equity: Decimal = Decimal("10000.00"),
    as_of: datetime,
    is_stale: bool = False,
) -> AccountState:
    return AccountState(
        account_id=account_id or uuid4(),
        broker="TestBroker",
        login="12345678",
        currency="USD",
        balance=balance,
        equity=equity,
        margin=Decimal("0"),
        free_margin=equity,
        margin_level=None,
        leverage=500,
        server_time=as_of,
        reported_at=as_of,
        is_stale=is_stale,
    )


def make_risk_state(*, as_of: datetime, trading_enabled: bool = True) -> RiskState:
    return RiskState(
        as_of=as_of,
        realised_pnl_today=Decimal("0"),
        realised_pnl_week=Decimal("0"),
        open_risk=Decimal("0"),
        trades_today=0,
        open_position_count=0,
        consecutive_losses=0,
        peak_equity=Decimal("10000.00"),
        current_drawdown_pct=Decimal("0"),
        trading_enabled=trading_enabled,
        kill_switch_active=not trading_enabled,
    )


def make_market_state(
    *,
    symbol: str = "XAUUSD",
    primary_tf: Timeframe = Timeframe.M15,
    as_of: datetime | None = None,
    bar_counts: dict[Timeframe, int] | None = None,
    session: Session = Session.LONDON,
    open_positions: tuple[Position, ...] = (),
    calendar_events: tuple[CalendarEvent, ...] = (),
    spec: SymbolSpec = XAUUSD_SPEC,
    trend: Decimal = Decimal("0"),
) -> MarketState:
    as_of = as_of or datetime(2026, 9, 7, 9, 15, tzinfo=UTC)
    bar_counts = bar_counts or {
        Timeframe.M15: 220,
        Timeframe.H1: 60,
        Timeframe.H4: 30,
        Timeframe.D1: 10,
    }

    bars: dict[Timeframe, tuple[Bar, ...]] = {}
    for tf, count in bar_counts.items():
        tf_seconds = tf.seconds
        last_open = (
            as_of - timedelta(seconds=tf_seconds)
            if tf == primary_tf
            else _last_closed_open(as_of, tf)
        )
        start = last_open - timedelta(seconds=tf_seconds * (count - 1))
        bars[tf] = make_bars(symbol=symbol, timeframe=tf, count=count, start=start, trend=trend)

    quote = Quote(
        symbol=symbol,
        bid=Decimal("3418.10"),
        ask=Decimal("3418.40"),
        server_time=as_of,
        received_at=as_of,
    )

    return MarketState(
        symbol=symbol,
        spec=spec,
        as_of=as_of,
        primary_tf=primary_tf,
        bars=bars,
        quote=quote,
        session=session,
        account=make_account_state(as_of=as_of),
        open_positions=open_positions,
        recent_signals=(),
        calendar_events=calendar_events,
        risk_state=make_risk_state(as_of=as_of),
    )


def _last_closed_open(as_of: datetime, tf: Timeframe) -> datetime:
    """The open_time of the most recently closed bar of timeframe tf as of as_of."""
    tf_seconds = tf.seconds
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (as_of - epoch).total_seconds()
    boundary_count = int(elapsed // tf_seconds)
    last_close = epoch + timedelta(seconds=boundary_count * tf_seconds)
    if last_close > as_of:
        last_close -= timedelta(seconds=tf_seconds)
    return last_close - timedelta(seconds=tf_seconds)


def make_trade_ready_state(
    *, as_of: datetime | None = None, session: Session = Session.LONDON_NY_OVERLAP
) -> MarketState:
    """A hand-engineered MarketState that clears every strategy gate in the
    default pipeline: a well-defined swing high (unimodal per oscillation
    cycle, so the fractal detector confirms it), a liquidity sweep-and-reject
    just below it, then a strong bullish breakout on the close bar. Used by
    the golden and determinism tests, which need at least one real TRADE
    decision to exercise the full pipeline end to end."""
    as_of = as_of or datetime(2026, 9, 7, 13, 15, tzinfo=UTC)
    primary_tf = Timeframe.M15
    t0 = as_of - timedelta(minutes=15 * 53)

    pattern = [1, 2, 3, 4, 5, 4, 3, 2, 1, 0]
    m15: list[Bar] = []
    for i in range(50):
        tt = t0 + timedelta(minutes=15 * i)
        mid = Decimal("3400") + Decimal(pattern[i % 10])
        m15.append(
            Bar(
                symbol="XAUUSD",
                timeframe=primary_tf,
                open_time=tt,
                open=mid - Decimal("0.3"),
                high=mid + Decimal("0.6"),
                low=mid - Decimal("0.6"),
                close=mid + Decimal("0.3"),
                tick_volume=100 + i,
                real_volume=None,
                spread_points=10,
            )
        )

    t_sweep = t0 + timedelta(minutes=15 * 50)
    t_reject = t_sweep + timedelta(minutes=15)
    t_break = t_reject + timedelta(minutes=15)
    m15.extend(
        [
            Bar(
                symbol="XAUUSD",
                timeframe=primary_tf,
                open_time=t_sweep,
                open=Decimal("3401"),
                high=Decimal("3401.2"),
                low=Decimal("3396"),
                close=Decimal("3397"),
                tick_volume=150,
                real_volume=None,
                spread_points=10,
            ),
            Bar(
                symbol="XAUUSD",
                timeframe=primary_tf,
                open_time=t_reject,
                open=Decimal("3397"),
                high=Decimal("3405"),
                low=Decimal("3396.5"),
                close=Decimal("3404"),
                tick_volume=150,
                real_volume=None,
                spread_points=10,
            ),
            Bar(
                symbol="XAUUSD",
                timeframe=primary_tf,
                open_time=t_break,
                open=Decimal("3404"),
                high=Decimal("3418"),
                low=Decimal("3403"),
                close=Decimal("3417"),
                tick_volume=200,
                real_volume=None,
                spread_points=10,
            ),
        ]
    )

    h4_bars: list[Bar] = []
    price = Decimal("3300")
    for i in range(60):
        tt = as_of - timedelta(hours=4 * (60 - i))
        close = price + Decimal("2")
        h4_bars.append(
            Bar(
                symbol="XAUUSD",
                timeframe=Timeframe.H4,
                open_time=tt,
                open=price,
                high=close + Decimal("0.2"),
                low=price - Decimal("0.2"),
                close=close,
                tick_volume=100,
                real_volume=None,
                spread_points=10,
            )
        )
        price = close

    d1_bars: list[Bar] = []
    price = Decimal("3200")
    for i in range(10):
        tt = as_of - timedelta(days=(10 - i))
        close = price + Decimal("15")
        d1_bars.append(
            Bar(
                symbol="XAUUSD",
                timeframe=Timeframe.D1,
                open_time=tt,
                open=price,
                high=close + Decimal("0.5"),
                low=price - Decimal("0.5"),
                close=close,
                tick_volume=100,
                real_volume=None,
                spread_points=10,
            )
        )
        price = close

    last_close = m15[-1].close
    quote = Quote(
        symbol="XAUUSD",
        bid=last_close - Decimal("0.05"),
        ask=last_close + Decimal("0.05"),
        server_time=as_of,
        received_at=as_of,
    )

    return MarketState(
        symbol="XAUUSD",
        spec=XAUUSD_SPEC,
        as_of=as_of,
        primary_tf=primary_tf,
        bars={
            Timeframe.M15: tuple(m15),
            Timeframe.H4: tuple(h4_bars),
            Timeframe.D1: tuple(d1_bars),
        },
        quote=quote,
        session=session,
        account=make_account_state(as_of=as_of),
        open_positions=(),
        recent_signals=(),
        calendar_events=(),
        risk_state=make_risk_state(as_of=as_of),
    )


def make_calendar_event(
    *,
    event_time: datetime,
    currency: str = "USD",
    impact: CalendarImpact = CalendarImpact.HIGH,
    title: str = "NFP",
) -> CalendarEvent:
    return CalendarEvent(event_time=event_time, currency=currency, impact=impact, title=title)
