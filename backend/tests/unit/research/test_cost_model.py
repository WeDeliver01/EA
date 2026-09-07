from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.market.enums import AssetClass, Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.research.cost_model import (
    CostModel,
    UnsupportedSlippageModel,
    commission_cost,
    slippage_price,
    spread_cost_price,
    swap_cost,
)

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
    stops_level_points=50,
    freeze_level_points=0,
    margin_initial=Decimal("1000"),
    currency_profit="USD",
    currency_margin="USD",
    quote_currency="USD",
)


def _cost_model(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "spread_source": "fixed",
        "fixed_spread_points": 20,
        "commission_per_lot_per_side": Decimal("3.5"),
        "swap_long_points": Decimal("-5"),
        "swap_short_points": Decimal("-2"),
        "triple_swap_weekday": 2,
        "slippage_model": "none",
        "slippage_points": None,
    }
    defaults.update(overrides)
    return CostModel(**defaults)  # type: ignore[arg-type]


def test_spread_cost_uses_fixed_points_when_configured() -> None:
    cfg = _cost_model(spread_source="fixed", fixed_spread_points=20)
    cost = spread_cost_price(_SPEC, cfg, recorded_spread_points=999)
    assert cost == Decimal("0.10")  # 20 points * 0.01 / 2


def test_spread_cost_uses_recorded_points_when_configured() -> None:
    cfg = _cost_model(spread_source="recorded")
    cost = spread_cost_price(_SPEC, cfg, recorded_spread_points=40)
    assert cost == Decimal("0.20")  # 40 * 0.01 / 2


def test_spread_cost_defaults_to_zero_when_recorded_points_missing() -> None:
    cfg = _cost_model(spread_source="recorded")
    assert spread_cost_price(_SPEC, cfg, recorded_spread_points=None) == Decimal("0")


def test_slippage_none_model_is_zero() -> None:
    cfg = _cost_model(slippage_model="none")
    assert slippage_price(_SPEC, cfg) == Decimal("0")


def test_slippage_fixed_model_uses_configured_points() -> None:
    cfg = _cost_model(slippage_model="fixed", slippage_points=10)
    assert slippage_price(_SPEC, cfg) == Decimal("0.10")


def test_slippage_measured_model_raises_unsupported() -> None:
    cfg = _cost_model(slippage_model="measured")
    with pytest.raises(UnsupportedSlippageModel):
        slippage_price(_SPEC, cfg)


def test_commission_cost_scales_with_volume() -> None:
    cfg = _cost_model(commission_per_lot_per_side=Decimal("3.5"))
    assert commission_cost(Decimal("2"), cfg) == Decimal("7.0")


def test_swap_cost_applies_long_points_per_night() -> None:
    cfg = _cost_model(swap_long_points=Decimal("-5"), triple_swap_weekday=2)
    nights = [date(2026, 9, 7), date(2026, 9, 8)]  # Monday, Tuesday - no triple
    cost = swap_cost(
        volume=Decimal("1"), direction=Direction.LONG, nights_held=nights, cfg=cfg, spec=_SPEC
    )
    assert cost == Decimal("-0.10")  # (-5 + -5) points * 0.01 * 1 lot


def test_swap_cost_triples_on_configured_weekday() -> None:
    cfg = _cost_model(swap_long_points=Decimal("-5"), triple_swap_weekday=2)
    nights = [date(2026, 9, 9)]  # a Wednesday
    cost = swap_cost(
        volume=Decimal("1"), direction=Direction.LONG, nights_held=nights, cfg=cfg, spec=_SPEC
    )
    assert cost == Decimal("-0.15")  # -5 * 3 points * 0.01


def test_swap_cost_uses_short_points_for_short_positions() -> None:
    cfg = _cost_model(swap_short_points=Decimal("-2"))
    nights = [date(2026, 9, 7)]
    cost = swap_cost(
        volume=Decimal("1"), direction=Direction.SHORT, nights_held=nights, cfg=cfg, spec=_SPEC
    )
    assert cost == Decimal("-0.02")


def test_swap_cost_with_no_nights_held_is_zero() -> None:
    cfg = _cost_model()
    cost = swap_cost(
        volume=Decimal("1"), direction=Direction.LONG, nights_held=[], cfg=cfg, spec=_SPEC
    )
    assert cost == Decimal("0")
