from __future__ import annotations

from decimal import Decimal

import pytest

from app.engines.config import StrategyConfig


@pytest.fixture
def trade_ready_config() -> StrategyConfig:
    """A StrategyConfig tuned so `factories.make_trade_ready_state()` clears
    every gate: the default tp_ladder's first rung (1R) is below the default
    min_rr (1.5), so both are widened slightly for this fixture."""
    base = StrategyConfig()
    trade_construction = base.trade_construction.model_copy(
        update={
            "tp_ladder": (
                (Decimal("1.5"), Decimal("0.5")),
                (Decimal("3.0"), Decimal("0.5")),
            )
        }
    )
    filters = base.filters.model_copy(update={"min_rr": Decimal("1.0")})
    return base.model_copy(update={"trade_construction": trade_construction, "filters": filters})
