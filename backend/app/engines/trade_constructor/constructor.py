"""SPEC-05 §3.8: TradeConstructor.

Simplification versus the spec's `tp_mode` enum: rather than branching on a
mode name, take-profits are always built directly from
`config.trade_construction.tp_ladder` - the R-multiple/fraction pairs already
fully describe a static ladder (`fixed_1r`, `partial_1r_runner`, etc are just
different ladders), and the two modes that aren't a static ladder at all
(`atr_trail`, `structure_trail`) are post-fill position management, not
something the engine decides at signal time (SPEC-06 §8). See
docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.market.enums import Direction
from app.domain.market.symbol_spec import SymbolSpec
from app.domain.strategy.decision import TakeProfit
from app.domain.strategy.setup import Setup
from app.engines.config import TradeConstructionConfig


@dataclass(frozen=True, slots=True)
class ConstructedTrade:
    entry: Decimal
    stop_loss: Decimal
    take_profits: tuple[TakeProfit, ...]
    rr: Decimal | None
    stop_distance_valid: bool
    stop_detail: dict[str, str]


def _sign(direction: Direction) -> Decimal:
    return Decimal(1) if direction == Direction.LONG else Decimal(-1)


class TradeConstructor:
    def __init__(self, config: TradeConstructionConfig) -> None:
        self._config = config

    def construct(self, setup: Setup, *, atr: Decimal, spec: SymbolSpec) -> ConstructedTrade:
        cfg = self._config
        sign = _sign(setup.direction)

        entry = (
            setup.trigger_price
        )  # entry_mode == 'market_on_close' is the only mode implemented at MVP scope

        structural_stop = setup.invalidation_price
        atr_stop = entry - sign * atr * cfg.atr_stop_multiple

        stop_by_mode = {
            "structural": structural_stop,
            "atr": atr_stop,
            "wider_of": max(structural_stop, atr_stop, key=lambda p: abs(entry - p)),
            "tighter_of": min(structural_stop, atr_stop, key=lambda p: abs(entry - p)),
        }
        stop_loss = stop_by_mode.get(cfg.stop_mode, structural_stop)

        distance = abs(entry - stop_loss)
        min_distance = cfg.min_stop_atr_multiple * atr if atr > 0 else Decimal(0)
        max_distance = cfg.max_stop_atr_multiple * atr if atr > 0 else Decimal(0)
        broker_min_distance = spec.stops_level_points * spec.point
        effective_min = max(min_distance, broker_min_distance)

        stop_distance_valid = atr > 0 and effective_min <= max_distance
        if stop_distance_valid:
            if distance < effective_min:
                distance = effective_min
            elif distance > max_distance:
                distance = max_distance
            stop_loss = entry - sign * distance

        take_profits: list[TakeProfit] = []
        for r_multiple, fraction in cfg.tp_ladder:
            level = entry + sign * r_multiple * distance
            take_profits.append(TakeProfit(level=level, fraction=fraction, r_multiple=r_multiple))

        rr = (
            abs(take_profits[0].level - entry) / distance if take_profits and distance > 0 else None
        )

        return ConstructedTrade(
            entry=entry,
            stop_loss=stop_loss,
            take_profits=tuple(take_profits),
            rr=rr,
            stop_distance_valid=stop_distance_valid,
            stop_detail={
                "distance": str(distance),
                "effective_min": str(effective_min),
                "max_distance": str(max_distance),
                "stop_mode": cfg.stop_mode,
            },
        )
