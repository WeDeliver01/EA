"""SPEC-06 §8 unit tests for `decide()` - the pure, DB-free management
logic. Mirrors `research/position_simulator.py`'s test approach: hand-picked
numbers so breakeven/rung/trailing/time-exit boundaries are exact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.enums import AssetClass, Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import TakeProfit
from app.engines.config import TradeConstructionConfig
from app.execution.position_manager import LivePosition, decide

pytestmark = pytest.mark.unit

_SPEC = SymbolSpec(
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
    stops_level_points=10,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)

_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _position(
    *,
    direction: Direction = Direction.LONG,
    entry: str = "3400",
    initial_stop: str = "3390",
    current_stop: str | None = None,
    take_profits: tuple[TakeProfit, ...] = (),
    partials_taken: int = 0,
    breakeven_moved: bool = False,
    volume: str = "0.10",
    remaining: str | None = None,
    opened_at: datetime = _START,
) -> LivePosition:
    return LivePosition(
        id=uuid.uuid4(),
        broker_position_id="1001",
        account_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        direction=direction,
        initial_volume=Decimal(volume),
        remaining_volume=Decimal(remaining if remaining is not None else volume),
        entry_price=Decimal(entry),
        initial_stop=Decimal(initial_stop),
        current_stop=Decimal(current_stop if current_stop is not None else initial_stop),
        take_profits=take_profits,
        partials_taken=partials_taken,
        breakeven_moved=breakeven_moved,
        opened_at=opened_at,
    )


def _cfg(**overrides: object) -> TradeConstructionConfig:
    return TradeConstructionConfig(**overrides)


def test_no_action_when_nothing_triggers() -> None:
    position = _position()
    action = decide(
        position,
        current_price=Decimal("3401"),
        atr=Decimal("5"),
        cfg=_cfg(breakeven_at_r=Decimal("1.0"), trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"


def test_breakeven_moves_the_stop_once_target_r_is_reached() -> None:
    # initial risk = 10 (3400-3390); breakeven_at_r=1.0 -> triggers at +10.
    position = _position()
    action = decide(
        position,
        current_price=Decimal("3410"),
        atr=Decimal("4"),
        cfg=_cfg(
            breakeven_at_r=Decimal("1.0"), breakeven_buffer_atr=Decimal("0.5"), trail_mode="none"
        ),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "modify_stop"
    assert action.sets_breakeven is True
    # 3400 + 0.5*4 = 3402
    assert action.new_stop == Decimal("3402")


def test_breakeven_does_not_fire_below_the_target_r() -> None:
    position = _position()
    action = decide(
        position,
        current_price=Decimal("3405"),  # only +5, target is +10
        atr=Decimal("4"),
        cfg=_cfg(breakeven_at_r=Decimal("1.0"), trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"


def test_take_profit_rung_fires_in_order_and_reports_the_correct_close_price() -> None:
    tps = (
        TakeProfit(level=Decimal("3410"), fraction=Decimal("0.5"), r_multiple=Decimal("1.0")),
        TakeProfit(level=Decimal("3420"), fraction=Decimal("0.5"), r_multiple=Decimal("2.0")),
    )
    position = _position(take_profits=tps)
    action = decide(
        position,
        current_price=Decimal("3410"),
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "close_partial"
    assert action.rung_index == 0
    assert action.close_price == Decimal("3410")
    assert action.close_volume == Decimal("0.05")


def test_take_profit_rung_already_taken_is_skipped() -> None:
    tps = (
        TakeProfit(level=Decimal("3410"), fraction=Decimal("0.5"), r_multiple=Decimal("1.0")),
        TakeProfit(level=Decimal("3420"), fraction=Decimal("0.5"), r_multiple=Decimal("2.0")),
    )
    position = _position(take_profits=tps, partials_taken=1, remaining="0.05", breakeven_moved=True)
    action = decide(
        position,
        current_price=Decimal("3420"),
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "close_partial"
    assert action.rung_index == 1
    assert action.close_volume == Decimal("0.05")  # remaining, not initial*fraction


def test_unreachable_rung_falls_through_to_other_checks() -> None:
    tps = (TakeProfit(level=Decimal("3420"), fraction=Decimal("1.0"), r_multiple=Decimal("2.0")),)
    position = _position(take_profits=tps)
    action = decide(
        position,
        current_price=Decimal("3401"),  # nowhere near 3420
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"


def test_trailing_activates_after_a_partial_without_breakeven_ever_moving() -> None:
    tps = (TakeProfit(level=Decimal("3410"), fraction=Decimal("1.0"), r_multiple=Decimal("1.0")),)
    position = _position(take_profits=tps, partials_taken=1, remaining="0", breakeven_moved=False)
    action = decide(
        position,
        current_price=Decimal("3415"),
        atr=Decimal("4"),
        # breakeven disabled (impossibly high target) so only the
        # partials-taken path can activate trailing here.
        cfg=_cfg(
            breakeven_at_r=Decimal("999"), trail_mode="atr", trail_atr_multiple=Decimal("2.0")
        ),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "modify_stop"
    assert action.sets_breakeven is False  # trailing, not the breakeven move itself
    # 3415 - 2*4 = 3407
    assert action.new_stop == Decimal("3407")


def test_trailing_never_widens_the_stop() -> None:
    tps = (TakeProfit(level=Decimal("3410"), fraction=Decimal("1.0"), r_multiple=Decimal("1.0")),)
    position = _position(
        take_profits=tps,
        partials_taken=1,
        remaining="0",
        breakeven_moved=True,
        current_stop="3408",
    )
    action = decide(
        position,
        current_price=Decimal("3409"),  # candidate would be 3409-8=3401, worse than 3408
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="atr", trail_atr_multiple=Decimal("2.0")),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"


def test_time_exit_fires_once_max_holding_duration_elapses() -> None:
    position = _position()
    action = decide(
        position,
        current_price=Decimal("3401"),
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(hours=24),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "close_full"
    assert action.close_reason == "TIME_EXIT"
    assert action.close_price == Decimal("3401")
    assert action.close_volume == position.remaining_volume


def test_time_exit_does_not_fire_before_the_duration_elapses() -> None:
    position = _position()
    action = decide(
        position,
        current_price=Decimal("3401"),
        atr=Decimal("4"),
        cfg=_cfg(trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(hours=23, minutes=59),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"


def test_short_direction_breakeven_and_rung_use_inverted_comparisons() -> None:
    position = _position(direction=Direction.SHORT, entry="3400", initial_stop="3410")
    action = decide(
        position,
        current_price=Decimal("3390"),  # -10 move, favorable for a short
        atr=Decimal("4"),
        cfg=_cfg(
            breakeven_at_r=Decimal("1.0"), breakeven_buffer_atr=Decimal("0.5"), trail_mode="none"
        ),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "modify_stop"
    # 3400 - 0.5*4 = 3398 (moves down for a short)
    assert action.new_stop == Decimal("3398")


def test_small_remaining_rung_is_skipped_when_below_volume_min() -> None:
    tps = (
        TakeProfit(level=Decimal("3410"), fraction=Decimal("0.5"), r_multiple=Decimal("1.0")),
        TakeProfit(level=Decimal("3420"), fraction=Decimal("0.5"), r_multiple=Decimal("2.0")),
    )
    # remaining is smaller than volume_min after flooring the first rung's
    # fraction - it must be skipped, not raise or return a zero-volume action.
    position = _position(take_profits=tps, volume="0.10", remaining="0.005")
    action = decide(
        position,
        current_price=Decimal("3410"),
        atr=Decimal("4"),
        # breakeven disabled so the rung-skip path is what's under test.
        cfg=_cfg(breakeven_at_r=Decimal("999"), trail_mode="none"),
        spec=_SPEC,
        as_of=_START + timedelta(minutes=15),
        max_holding_duration=timedelta(hours=24),
    )
    assert action.kind == "none"
