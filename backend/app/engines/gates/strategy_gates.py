"""SPEC-05 §3.7: GateEvaluator (strategy gates only).

All gates are evaluated even after the first failure - short-circuiting
destroys the telemetry that answers "why is the bot not trading" (SPEC-05
§3.7). Execution and risk gates (DAILY_LOSS_LIMIT, AGENT_DISCONNECTED, ...)
are not here; they run in the impure risk engine and execution guard.

Sequencing note: the pipeline diagram in SPEC-05 §2 places GateEvaluator
(step 8) before TradeConstructor (step 9), but two of its gates -
RR_BELOW_MINIMUM and STOP_DISTANCE_INVALID - need the entry/stop that only
TradeConstructor produces. `evaluate_pre_construction` runs at step 8 with
everything that doesn't need a constructed trade; `evaluate_post_construction`
runs right after TradeConstructor and both results land on the same
`Decision.gates` tuple. See docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain.market.market_state import MarketState
from app.domain.strategy.enums import GateCode
from app.domain.strategy.gate_result import GateResult
from app.domain.strategy.setup import Setup
from app.engines.config import ConfluenceConfig, ContextConfig, FiltersConfig, SetupsConfig
from app.engines.context.types import Context


def evaluate_pre_construction(
    state: MarketState,
    context: Context,
    confluence_score: Decimal,
    setup: Setup | None,
    *,
    confluence_cfg: ConfluenceConfig,
    context_cfg: ContextConfig,
    filters_cfg: FiltersConfig,
    setups_cfg: SetupsConfig,
) -> tuple[GateResult, ...]:
    gates: list[GateResult] = []

    confluence_ok = confluence_score >= confluence_cfg.minimum_to_trade
    gates.append(
        GateResult(
            code=GateCode.CONFLUENCE_BELOW_THRESHOLD,
            passed=confluence_ok,
            detail={
                "score": str(confluence_score),
                "minimum": str(confluence_cfg.minimum_to_trade),
            },
        )
    )

    regime_ok = context.regime in context_cfg.permitted_regimes
    gates.append(
        GateResult(
            code=GateCode.REGIME_NOT_PERMITTED,
            passed=regime_ok,
            detail={"regime": context.regime.value},
        )
    )

    session_ok = state.session in filters_cfg.sessions
    gates.append(
        GateResult(
            code=GateCode.SESSION_NOT_PERMITTED,
            passed=session_ok,
            detail={"session": state.session.value},
        )
    )

    duplicate_ok = True
    if setup is not None:
        primary_tf_seconds = state.primary_tf.seconds
        cutoff = state.as_of - timedelta(
            seconds=primary_tf_seconds * setups_cfg.duplicate_window_bars
        )
        duplicate_ok = not any(
            s.setup_fingerprint == setup.fingerprint and s.created_at >= cutoff
            for s in state.recent_signals
        )
    gates.append(GateResult(code=GateCode.DUPLICATE_SETUP, passed=duplicate_ok, detail={}))

    blackout_impacts = set(filters_cfg.news_blackout_impacts)
    news_ok = not any(e.impact.value in blackout_impacts for e in state.calendar_events)
    gates.append(
        GateResult(
            code=GateCode.NEWS_BLACKOUT,
            passed=news_ok,
            detail={"events_in_window": len(state.calendar_events)},
        )
    )

    from app.domain.market.enums import Regime  # local import: avoids a cycle at module import time

    volatility_ok = context.regime != Regime.UNKNOWN
    gates.append(
        GateResult(
            code=GateCode.VOLATILITY_OUT_OF_BOUNDS,
            passed=volatility_ok,
            detail={"regime": context.regime.value},
        )
    )

    primary_atr = context.atr.get(state.primary_tf)
    if primary_atr is not None and primary_atr > 0:
        max_spread = filters_cfg.max_spread_atr_multiple * primary_atr
        spread_ok = state.quote.spread <= max_spread
    else:
        spread_ok = False
    gates.append(
        GateResult(
            code=GateCode.SPREAD_TOO_WIDE,
            passed=spread_ok,
            detail={"spread": str(state.quote.spread)},
        )
    )

    return tuple(gates)


def evaluate_post_construction(
    *, rr: Decimal | None, min_rr: Decimal, stop_distance_valid: bool, stop_detail: dict[str, str]
) -> tuple[GateResult, ...]:
    rr_ok = rr is not None and rr >= min_rr
    return (
        GateResult(
            code=GateCode.RR_BELOW_MINIMUM,
            passed=rr_ok,
            detail={"rr": str(rr) if rr is not None else None, "minimum": str(min_rr)},
        ),
        GateResult(
            code=GateCode.STOP_DISTANCE_INVALID, passed=stop_distance_valid, detail=stop_detail
        ),
    )
