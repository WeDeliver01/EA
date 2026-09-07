"""SPEC-06 §2 required tests: property (risk_amount_actual <= target),
table test across instrument/account-currency combinations, boundary
conditions, and the "1-pip stop on a large account" regression. Also drives
100% branch coverage on `engines/risk/sizing.py` (SPEC-10 Phase 2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.exceptions import InvalidContractSpec, InvalidStopDistance
from app.domain.market.enums import AssetClass
from app.domain.market.symbol_spec import SymbolSpec
from app.engines.risk.sizing import calculate_size, estimate_margin, floor_to_step

pytestmark = pytest.mark.unit


def _spec(**overrides: object) -> SymbolSpec:
    defaults: dict[str, object] = {
        "symbol": "XAUUSD",
        "asset_class": AssetClass.METAL,
        "digits": 2,
        "point": Decimal("0.01"),
        "tick_size": Decimal("0.01"),
        "tick_value": Decimal("1.00"),
        "contract_size": Decimal("100"),
        "volume_min": Decimal("0.01"),
        "volume_max": Decimal("50"),
        "volume_step": Decimal("0.01"),
        "stops_level_points": 50,
        "freeze_level_points": 0,
        "margin_initial": Decimal("1000"),
        "currency_profit": "USD",
        "currency_margin": "USD",
        "quote_currency": "USD",
    }
    defaults.update(overrides)
    return SymbolSpec(**defaults)  # type: ignore[arg-type]


def _calc(**overrides: object):
    defaults: dict[str, object] = {
        "account_equity": Decimal("10000"),
        "account_free_margin": Decimal("10000"),
        "risk_pct": Decimal("0.005"),
        "entry": Decimal("3418.20"),
        "stop_loss": Decimal("3412.55"),
        "spec": _spec(),
        "conversion_rate": Decimal("1"),
        "leverage": 500,
        "max_lot_size": Decimal("5"),
        "margin_safety_factor": Decimal("0.30"),
    }
    defaults.update(overrides)
    return calculate_size(**defaults)  # type: ignore[arg-type]


# ---- floor_to_step / estimate_margin


def test_floor_to_step_rounds_down_never_up() -> None:
    assert floor_to_step(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert floor_to_step(Decimal("1.230"), Decimal("0.01")) == Decimal("1.23")


def test_floor_to_step_rejects_non_positive_step() -> None:
    with pytest.raises(ValueError, match="step must be positive"):
        floor_to_step(Decimal("1"), Decimal("0"))


def test_estimate_margin_rejects_non_positive_leverage() -> None:
    with pytest.raises(ValueError, match="leverage must be positive"):
        estimate_margin(volume=Decimal("1"), spec=_spec(), entry=Decimal("100"), leverage=0)


# ---- calculate_size: exceptions


def test_zero_stop_distance_raises_invalid_stop_distance() -> None:
    with pytest.raises(InvalidStopDistance):
        _calc(entry=Decimal("100"), stop_loss=Decimal("100"))


def test_zero_tick_value_raises_invalid_contract_spec() -> None:
    with pytest.raises(InvalidContractSpec):
        _calc(spec=_spec(tick_value=Decimal("0")))


# ---- calculate_size: blocked outcomes


def test_volume_below_minimum_is_blocked() -> None:
    # Tiny risk budget against a large stop distance floors to 0 lots.
    outcome = _calc(
        account_equity=Decimal("10"), risk_pct=Decimal("0.001"), stop_loss=Decimal("3018.20")
    )
    assert outcome.approved is False
    assert outcome.size is None
    assert outcome.block_reason == "BELOW_MIN_VOLUME"


def test_insufficient_margin_is_blocked() -> None:
    outcome = _calc(account_free_margin=Decimal("1"), leverage=1)
    assert outcome.approved is False
    assert outcome.size is None
    assert outcome.block_reason == "INSUFFICIENT_MARGIN"


# ---- calculate_size: approved path, and the clamps in step 11


def test_approved_outcome_has_a_fully_populated_position_size() -> None:
    outcome = _calc()
    assert outcome.approved is True
    assert outcome.block_reason is None
    size = outcome.size
    assert size is not None
    assert size.volume > 0
    assert size.risk_amount > 0
    assert size.margin_required > 0
    assert "raw_volume" in size.calculation


def test_a_tiny_stop_on_a_large_account_is_capped_by_max_lot_size() -> None:
    # A 1-tick stop with a large risk budget would naively size to hundreds
    # of lots; max_lot_size must cap it, not volume_max.
    outcome = _calc(
        account_equity=Decimal("10_000_000"),
        account_free_margin=Decimal("10_000_000"),
        risk_pct=Decimal("0.005"),
        stop_loss=Decimal("3418.19"),
        max_lot_size=Decimal("5"),
    )
    assert outcome.approved is True
    assert outcome.size is not None
    assert outcome.size.volume == Decimal("5")


def test_volume_is_capped_by_broker_volume_max_when_lower_than_max_lot_size() -> None:
    outcome = _calc(
        account_equity=Decimal("10_000_000"),
        risk_pct=Decimal("0.005"),
        stop_loss=Decimal("3418.19"),
        spec=_spec(volume_max=Decimal("2")),
        max_lot_size=Decimal("5"),
    )
    assert outcome.approved is True
    assert outcome.size is not None
    assert outcome.size.volume == Decimal("2")


# ---- property test: actual risk never exceeds the target


@given(
    equity=st.decimals(
        min_value="100", max_value="1000000", places=2, allow_nan=False, allow_infinity=False
    ),
    risk_pct=st.decimals(
        min_value="0.001", max_value="0.05", places=4, allow_nan=False, allow_infinity=False
    ),
    stop_points=st.integers(min_value=1, max_value=10000),
)
def test_risk_amount_actual_never_exceeds_target(
    equity: Decimal, risk_pct: Decimal, stop_points: int
) -> None:
    spec = _spec()
    entry = Decimal("3418.20")
    stop_loss = entry - Decimal(stop_points) * spec.point
    outcome = calculate_size(
        account_equity=equity,
        account_free_margin=equity,
        risk_pct=risk_pct,
        entry=entry,
        stop_loss=stop_loss,
        spec=spec,
        conversion_rate=Decimal("1"),
        leverage=500,
        max_lot_size=Decimal("1000"),
        margin_safety_factor=Decimal("1"),
    )
    if outcome.approved:
        assert outcome.size is not None
        assert outcome.size.risk_amount <= equity * risk_pct


# ---- table test: instrument / account-currency combinations (SPEC-06 §2)


@pytest.mark.parametrize(
    "name,spec,entry,stop_loss,conversion_rate,account_equity,leverage",
    [
        (
            "XAUUSD_zar_account",
            _spec(),
            Decimal("3418.20"),
            Decimal("3412.55"),
            Decimal("18.50"),  # USD -> ZAR
            Decimal("200000"),  # ZAR - realistic account size once converted
            500,
        ),
        (
            "EURUSD_usd_account",
            _spec(
                symbol="EURUSD",
                asset_class=AssetClass.FX,
                digits=5,
                point=Decimal("0.00001"),
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1.00"),
                contract_size=Decimal("100000"),
                volume_min=Decimal("0.01"),
                volume_step=Decimal("0.01"),
            ),
            Decimal("1.08500"),
            Decimal("1.08300"),
            Decimal("1"),
            Decimal("10000"),
            500,
        ),
        (
            "USDJPY_usd_account_quote_is_profit_currency",
            _spec(
                symbol="USDJPY",
                asset_class=AssetClass.FX,
                digits=3,
                point=Decimal("0.001"),
                tick_size=Decimal("0.001"),
                tick_value=Decimal("0.67"),  # pre-converted to account currency
                contract_size=Decimal("100000"),
                volume_min=Decimal("0.01"),
                volume_step=Decimal("0.01"),
            ),
            Decimal("149.500"),
            Decimal("147.500"),
            Decimal("1"),
            Decimal("10000"),
            500,
        ),
        (
            "GBPJPY_cross_zar_account",
            _spec(
                symbol="GBPJPY",
                asset_class=AssetClass.FX,
                digits=3,
                point=Decimal("0.001"),
                tick_size=Decimal("0.001"),
                tick_value=Decimal("0.67"),
                contract_size=Decimal("100000"),
                volume_min=Decimal("0.01"),
                volume_step=Decimal("0.01"),
            ),
            Decimal("190.500"),
            Decimal("190.100"),
            Decimal("18.50"),
            Decimal("200000"),
            500,
        ),
        (
            "US30_index_usd_account",
            _spec(
                symbol="US30",
                asset_class=AssetClass.INDEX,
                digits=1,
                point=Decimal("0.1"),
                tick_size=Decimal("0.1"),
                tick_value=Decimal("1.00"),
                contract_size=Decimal("1"),
                volume_min=Decimal("0.1"),
                volume_step=Decimal("0.1"),
            ),
            Decimal("39500.0"),
            Decimal("39450.0"),
            Decimal("1"),
            Decimal("10000"),
            20,
        ),
        (
            "XAUUSD_usd_account",
            _spec(),
            Decimal("3418.20"),
            Decimal("3412.55"),
            Decimal("1"),
            Decimal("10000"),
            500,
        ),
    ],
)
def test_sizing_table_across_instruments_and_account_currencies(
    name: str,
    spec: SymbolSpec,
    entry: Decimal,
    stop_loss: Decimal,
    conversion_rate: Decimal,
    account_equity: Decimal,
    leverage: int,
) -> None:
    outcome = calculate_size(
        account_equity=account_equity,
        account_free_margin=account_equity,
        risk_pct=Decimal("0.005"),
        entry=entry,
        stop_loss=stop_loss,
        spec=spec,
        conversion_rate=conversion_rate,
        leverage=leverage,
        max_lot_size=Decimal("50"),
        margin_safety_factor=Decimal("0.30"),
    )
    assert outcome.approved is True, name
    assert outcome.size is not None
    # Hand re-derivation of steps 1-9, independent of the implementation.
    risk_amount_target = account_equity * Decimal("0.005")
    stop_distance = abs(entry - stop_loss)
    stop_distance_ticks = stop_distance / spec.tick_size
    loss_per_lot = stop_distance_ticks * spec.tick_value * conversion_rate
    expected_raw_volume = risk_amount_target / loss_per_lot
    expected_volume = floor_to_step(expected_raw_volume, spec.volume_step)
    assert outcome.size.volume == expected_volume, name
    assert outcome.size.risk_amount <= risk_amount_target, name


# ---- boundary: volume_step of 0.01 and 0.1


@pytest.mark.parametrize("volume_step", [Decimal("0.01"), Decimal("0.1")])
def test_volume_step_boundaries(volume_step: Decimal) -> None:
    outcome = _calc(
        account_equity=Decimal("200000"),
        account_free_margin=Decimal("200000"),
        spec=_spec(volume_step=volume_step, volume_min=volume_step),
    )
    assert outcome.approved is True
    assert outcome.size is not None
    assert (outcome.size.volume / volume_step) == (
        outcome.size.volume / volume_step
    ).to_integral_value()
