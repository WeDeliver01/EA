"""SPEC-05 §3.6: EvidenceCollector.

Produces one `Evidence` per weighted type in `config.evidence_weights`.
Handlers exist for the seven types in the default config
(`docs/adr/0001-mvp-scope.md` records SR_ZONE, FIB_GOLDEN_ZONE,
VOLUME_CONFIRMATION, VOLATILITY_REGIME and SESSION_QUALITY as deferred - they
need data, real volume and a fib-swing model, that this MVP's synthetic
pipeline doesn't yet produce). A config that references a type with no
handler here simply gets no evidence row for it, which scores as absent.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from app.domain.market.market_state import MarketState
from app.domain.strategy.enums import EvidenceType
from app.domain.strategy.evidence import Evidence
from app.domain.strategy.setup import Setup
from app.engines.context.types import Context
from app.engines.manipulation.types import Manipulation
from app.engines.structure.types import Structure

_CANDLE_CONFIRMATION_BODY_RATIO = Decimal("0.5")

_EvidenceCheck = Callable[[MarketState, Context, Structure, Manipulation, Setup], bool]


def _htf_trend_alignment(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    return context.htf_bias is not None and context.htf_bias == setup.direction


def _liquidity_sweep(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    return manipulation.swept_pool is not None and manipulation.direction == setup.direction


def _manipulation_quality(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    return manipulation.quality > 0 and manipulation.direction == setup.direction


def _structure_break(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    return structure.last_break is not None and structure.last_break.direction == setup.direction


def _retest_confirmed(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    return setup.kind == "BREAKOUT_RETEST"


def _candle_confirmation(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    primary_bars = state.bars.get(state.primary_tf, ())
    if not primary_bars:
        return False
    last_bar = primary_bars[-1]
    if last_bar.body_ratio < _CANDLE_CONFIRMATION_BODY_RATIO:
        return False
    bullish = last_bar.close > last_bar.open
    return bullish == (setup.direction.value == "LONG")


def _momentum_expansion(
    state: MarketState,
    context: Context,
    structure: Structure,
    manipulation: Manipulation,
    setup: Setup,
) -> bool:
    from app.domain.market.enums import Regime  # local import: avoids a cycle at module import time

    return context.atr_expanding and context.regime == Regime.EXPANSION


_CHECKS: dict[EvidenceType, _EvidenceCheck] = {
    EvidenceType.HTF_TREND_ALIGNMENT: _htf_trend_alignment,
    EvidenceType.LIQUIDITY_SWEEP: _liquidity_sweep,
    EvidenceType.MANIPULATION_QUALITY: _manipulation_quality,
    EvidenceType.STRUCTURE_BREAK: _structure_break,
    EvidenceType.RETEST_CONFIRMED: _retest_confirmed,
    EvidenceType.CANDLE_CONFIRMATION: _candle_confirmation,
    EvidenceType.MOMENTUM_EXPANSION: _momentum_expansion,
}


class EvidenceCollector:
    def __init__(self, weights: dict[str, Decimal]) -> None:
        self._weights = weights

    def evaluate(
        self,
        state: MarketState,
        context: Context,
        structure: Structure,
        manipulation: Manipulation,
        setup: Setup,
    ) -> tuple[Evidence, ...]:
        evidence: list[Evidence] = []
        for type_name, weight in self._weights.items():
            try:
                evidence_type = EvidenceType(type_name)
            except ValueError:
                continue
            check = _CHECKS.get(evidence_type)
            if check is None:
                continue
            present = check(state, context, structure, manipulation, setup)
            evidence.append(
                Evidence(
                    type=evidence_type,
                    direction=setup.direction if present else None,
                    present=present,
                    weight=weight,
                    score=weight if present else Decimal(0),
                    detail={},
                )
            )
        return tuple(evidence)
