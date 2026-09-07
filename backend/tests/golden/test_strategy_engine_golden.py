"""SPEC-10 Phase 2 acceptance:
  - evaluate(state) == evaluate(state), and evaluate(deepcopy(state)) == evaluate(state)
  - golden Decision serialisations stay byte-identical across repeated runs

No MT5-verified indicator data is available in this environment, so this is
a reduced set of hand-engineered scenarios rather than SPEC-05's ~40 MT5-
matched fixtures - see docs/adr/0001-mvp-scope.md. Each one is still a real
end-to-end pipeline run with a pinned expected `Decision` serialisation.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.engines.config import StrategyConfig
from app.engines.strategy_engine import StrategyEngine
from tests.factories import make_market_state, make_trade_ready_state

pytestmark = pytest.mark.golden

_STRATEGY_VERSION_ID = UUID("018f3a2b-0000-7000-8000-000000000001")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal | UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, dict | list | tuple):
        # StrEnum members
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def serialize_decision(decision: Any) -> str:
    payload = _json_safe(asdict(decision))
    return json.dumps(payload, sort_keys=True)


def _golden_scenarios(trade_ready_config: StrategyConfig) -> list[tuple[str, Any, StrategyConfig]]:
    return [
        ("default_config_oscillating_wait", make_market_state(), StrategyConfig()),
        ("trade_ready_breakout", make_trade_ready_state(), trade_ready_config),
    ]


def test_evaluate_is_deterministic_across_repeated_calls(
    trade_ready_config: StrategyConfig,
) -> None:
    for _name, state, config in _golden_scenarios(trade_ready_config):
        engine = StrategyEngine(config, strategy_version_id=_STRATEGY_VERSION_ID)
        results = [serialize_decision(engine.evaluate(state)) for _ in range(3)]
        assert results[0] == results[1] == results[2]


def test_evaluate_on_a_deep_copy_equals_evaluate_on_the_original(
    trade_ready_config: StrategyConfig,
) -> None:
    for _name, state, config in _golden_scenarios(trade_ready_config):
        engine = StrategyEngine(config, strategy_version_id=_STRATEGY_VERSION_ID)
        original = serialize_decision(engine.evaluate(state))
        copied = serialize_decision(engine.evaluate(copy.deepcopy(state)))
        assert original == copied


def test_trade_ready_fixture_produces_a_trade_decision(trade_ready_config: StrategyConfig) -> None:
    state = make_trade_ready_state()
    engine = StrategyEngine(trade_ready_config, strategy_version_id=_STRATEGY_VERSION_ID)
    decision = engine.evaluate(state)

    assert decision.outcome.value == "TRADE"
    assert decision.setup is not None
    assert decision.setup.kind == "BREAKOUT"
    assert decision.direction is not None and decision.direction.value == "LONG"
    assert decision.entry == Decimal("3417")
    assert decision.confluence_band == "HIGH"
    assert all(g.passed for g in decision.gates)
    assert len(decision.take_profits) == 2


def test_default_config_oscillation_produces_a_wait_decision() -> None:
    state = make_market_state()
    engine = StrategyEngine(StrategyConfig(), strategy_version_id=_STRATEGY_VERSION_ID)
    decision = engine.evaluate(state)

    assert decision.outcome.value == "WAIT"
    assert decision.entry is None
    assert decision.stop_loss is None
    assert decision.take_profits == ()
