"""SPEC-06 §2: position sizing. The single most bug-prone calculation in
retail trading systems - specified exactly, in Decimal at every step (P8).

`BELOW_MIN_VOLUME` and `INSUFFICIENT_MARGIN` aren't in `GateCode`
(`INSUFFICIENT_MARGIN` is, but as a risk *gate* evaluated by the impure risk
engine in SPEC-06 §3, not as a sizing-function return value) - so a blocked
outcome here is returned as `SizingOutcome.block_reason`, a plain string,
which the impure caller translates into whatever gate/error representation
it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.domain.exceptions import InvalidContractSpec, InvalidStopDistance
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.risk.sizing import PositionSize


@dataclass(frozen=True, slots=True)
class SizingOutcome:
    approved: bool
    size: PositionSize | None
    block_reason: str | None  # "BELOW_MIN_VOLUME" | "INSUFFICIENT_MARGIN" | None


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def estimate_margin(*, volume: Decimal, spec: SymbolSpec, entry: Decimal, leverage: int) -> Decimal:
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    notional = volume * spec.contract_size * entry
    return notional / leverage


def calculate_size(
    *,
    account_equity: Decimal,
    account_free_margin: Decimal,
    risk_pct: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    spec: SymbolSpec,
    conversion_rate: Decimal,
    leverage: int,
    max_lot_size: Decimal,
    margin_safety_factor: Decimal,
) -> SizingOutcome:
    # 1
    risk_amount_target = account_equity * risk_pct
    # 2-3
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        raise InvalidStopDistance("entry and stop_loss must differ")
    # 4-5
    stop_distance_ticks = stop_distance / spec.tick_size
    value_per_tick_per_lot = spec.tick_value * conversion_rate
    # 6-7
    loss_per_lot = stop_distance_ticks * value_per_tick_per_lot
    if loss_per_lot <= 0:
        raise InvalidContractSpec("loss_per_lot must be positive - check tick_value/tick_size")
    # 8-9: ALWAYS floor. Rounding up on a risk target silently exceeds the limit.
    raw_volume = risk_amount_target / loss_per_lot
    volume = floor_to_step(raw_volume, spec.volume_step)
    # 10
    if volume < spec.volume_min:
        return SizingOutcome(approved=False, size=None, block_reason="BELOW_MIN_VOLUME")
    # 11
    volume = min(volume, spec.volume_max, max_lot_size)
    # 12-13
    risk_amount_actual = volume * loss_per_lot
    risk_pct_actual = risk_amount_actual / account_equity
    # 14
    margin_required = estimate_margin(volume=volume, spec=spec, entry=entry, leverage=leverage)
    # 15
    if margin_required > account_free_margin * margin_safety_factor:
        return SizingOutcome(approved=False, size=None, block_reason="INSUFFICIENT_MARGIN")

    size = PositionSize(
        volume=volume,
        risk_amount=risk_amount_actual,
        risk_pct_actual=risk_pct_actual,
        stop_distance_price=stop_distance,
        stop_distance_points=int(stop_distance / spec.point) if spec.point > 0 else 0,
        value_per_point_per_lot=value_per_tick_per_lot,
        margin_required=margin_required,
        calculation={
            "risk_amount_target": str(risk_amount_target),
            "stop_distance": str(stop_distance),
            "stop_distance_ticks": str(stop_distance_ticks),
            "value_per_tick_per_lot": str(value_per_tick_per_lot),
            "loss_per_lot": str(loss_per_lot),
            "raw_volume": str(raw_volume),
            "volume": str(volume),
            "risk_amount_actual": str(risk_amount_actual),
            "risk_pct_actual": str(risk_pct_actual),
            "margin_required": str(margin_required),
            "margin_safety_factor": str(margin_safety_factor),
        },
    )
    return SizingOutcome(approved=True, size=size, block_reason=None)
