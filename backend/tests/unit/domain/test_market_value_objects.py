from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import Timeframe
from app.domain.market.quote import Quote

pytestmark = pytest.mark.unit


def _bar(**overrides: object) -> Bar:
    defaults: dict[str, object] = {
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M15,
        "open_time": datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
        "open": Decimal("3418.20"),
        "high": Decimal("3419.00"),
        "low": Decimal("3417.50"),
        "close": Decimal("3418.80"),
        "tick_volume": 120,
        "real_volume": None,
        "spread_points": 20,
    }
    defaults.update(overrides)
    return Bar(**defaults)  # type: ignore[arg-type]


def test_bar_close_time_is_open_time_plus_timeframe() -> None:
    bar = _bar()
    assert bar.close_time == datetime(2026, 9, 7, 9, 15, tzinfo=UTC)


def test_bar_body_and_range_and_body_ratio() -> None:
    bar = _bar()
    assert bar.body == Decimal("0.60")
    assert bar.range == Decimal("1.50")
    assert bar.body_ratio == Decimal("0.60") / Decimal("1.50")


def test_bar_body_ratio_is_zero_when_range_is_zero() -> None:
    bar = _bar(
        high=Decimal("3418.20"),
        low=Decimal("3418.20"),
        open=Decimal("3418.20"),
        close=Decimal("3418.20"),
    )
    assert bar.body_ratio == Decimal(0)


def test_bar_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _bar(open_time=datetime(2026, 9, 7, 9, 0))


def test_bar_rejects_low_greater_than_high() -> None:
    with pytest.raises(ValueError, match="low cannot exceed"):
        _bar(low=Decimal("3420.00"), high=Decimal("3419.00"))


def test_bar_rejects_open_outside_range() -> None:
    with pytest.raises(ValueError, match="open must lie within"):
        _bar(open=Decimal("3500.00"))


def test_quote_spread_and_mid() -> None:
    now = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)
    quote = Quote(
        symbol="XAUUSD",
        bid=Decimal("3418.10"),
        ask=Decimal("3418.40"),
        server_time=now,
        received_at=now,
    )
    assert quote.spread == Decimal("0.30")
    assert quote.mid == Decimal("3418.25")


def test_quote_rejects_ask_below_bid() -> None:
    now = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="ask cannot be less than"):
        Quote(
            symbol="XAUUSD",
            bid=Decimal("3418.40"),
            ask=Decimal("3418.10"),
            server_time=now,
            received_at=now,
        )
