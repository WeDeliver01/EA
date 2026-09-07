"""SPEC-05 §3.9: NarrativeGenerator.

Deterministic, template-based. No language model, no randomness. For WAIT
decisions the narrative leads with the blocking gates - that's what makes
the WAIT log readable instead of a wall of JSON.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.market_state import MarketState
from app.domain.strategy.decision import TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.domain.strategy.gate_result import GateResult
from app.domain.strategy.setup import Setup
from app.engines.context.types import Context


def generate(
    *,
    state: MarketState,
    context: Context,
    outcome: DecisionOutcome,
    setup: Setup | None,
    entry: Decimal | None,
    stop_loss: Decimal | None,
    take_profits: tuple[TakeProfit, ...],
    confluence_score: Decimal,
    confluence_band: str,
    gates: tuple[GateResult, ...],
) -> str:
    header = (
        f"{state.symbol} {state.primary_tf.value} at {state.as_of.strftime('%Y-%m-%d %H:%M')} UTC. "
        f"Regime {context.regime.value}"
    )
    if context.htf_bias is not None:
        header += f", H4/HTF {context.htf_bias.value.lower()}"
    if context.itf_bias is not None:
        header += f", H1/ITF {context.itf_bias.value.lower()}"
    header += "."

    blocking = tuple(g for g in gates if not g.passed)

    confluence_text = f"Confluence {confluence_score:.1f} of 10 ({confluence_band})"
    if outcome == DecisionOutcome.WAIT:
        if not blocking:
            return f"{header} {confluence_text}. No setup detected."
        gate_list = ", ".join(g.code.value for g in blocking)
        return f"{header} Blocked by: {gate_list}. {confluence_text}."

    assert (
        setup is not None and entry is not None and stop_loss is not None
    )  # narrowed by TRADE outcome
    direction_word = "Long" if setup.direction.value == "LONG" else "Short"
    first_tp = take_profits[0] if take_profits else None
    tp_text = (
        f", first target {first_tp.level} at {first_tp.r_multiple}R" if first_tp is not None else ""
    )
    return (
        f"{header} {setup.kind.replace('_', ' ').title()} setup at {setup.reference_level}. "
        f"Confluence {confluence_score:.1f} of 10 ({confluence_band}). "
        f"{direction_word} from {entry}, stop {stop_loss}{tp_text}. Passed all gates."
    )
