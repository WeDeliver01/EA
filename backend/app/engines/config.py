"""SPEC-05 §4: StrategyConfig.

Every threshold that drives the pipeline lives here, never as a literal in
engine code. The engine's git SHA plus this config's SHA-256 is a strategy
version's identity (SPEC-01 §9); changing any value here is a new version,
never an in-place edit of a live one.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.domain.market.enums import Regime, Session


class ContextConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    atr_period: int = 14
    atr_percentile_lookback: int = 200
    adx_period: int = 14
    adx_trend_threshold: Decimal = Decimal("25")
    compression_atr_percentile: Decimal = Decimal("25")
    expansion_atr_percentile: Decimal = Decimal("75")
    permitted_regimes: tuple[Regime, ...] = (
        Regime.TRENDING_UP,
        Regime.TRENDING_DOWN,
        Regime.EXPANSION,
    )


class StructureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    swing_lookback: int = 3
    break_on_wick: bool = False
    range_min_bars: int = 8
    key_levels: tuple[str, ...] = ("PDH", "PDL", "PWH", "PWL", "SESSION_HIGH", "SESSION_LOW")


class LiquidityConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    equal_level_tolerance_atr: Decimal = Decimal("0.15")
    min_touches: int = 2
    lookback_bars: int = 200


class ManipulationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_bars_beyond_level: int = 3
    min_rejection_body_ratio: Decimal = Decimal("0.55")
    min_displacement_atr: Decimal = Decimal("0.8")
    require_session: tuple[Session, ...] = (
        Session.LONDON,
        Session.LONDON_NY_OVERLAP,
        Session.NEW_YORK,
    )


class SetupsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: tuple[str, ...] = ("BREAKOUT_RETEST", "BREAKOUT")
    retest_max_bars: int = 6
    retest_tolerance_atr: Decimal = Decimal("0.2")
    duplicate_window_bars: int = 24


class ConfluenceBands(BaseModel):
    model_config = ConfigDict(frozen=True)

    weak: Decimal = Decimal("6.0")
    valid: Decimal = Decimal("7.0")
    high: Decimal = Decimal("8.0")


class ConfluenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    bands: ConfluenceBands = ConfluenceBands()
    minimum_to_trade: Decimal = Decimal("7.0")


class TradeConstructionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_mode: str = "market_on_close"
    stop_mode: str = "wider_of"  # structural | atr | wider_of | tighter_of
    atr_stop_multiple: Decimal = Decimal("1.5")
    min_stop_atr_multiple: Decimal = Decimal("0.8")
    max_stop_atr_multiple: Decimal = Decimal("3.0")
    breakeven_at_r: Decimal = Decimal("1.0")
    breakeven_buffer_atr: Decimal = Decimal("0.1")
    trail_mode: str = "atr"  # "" | "none" | "atr" | "structure"
    trail_atr_multiple: Decimal = Decimal("2.0")
    max_holding_bars: int = 96
    tp_mode: str = "partial_1r_runner"
    tp_ladder: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("1.0"), Decimal("0.5")),
        (Decimal("3.0"), Decimal("0.5")),
    )


class FiltersConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: tuple[Session, ...] = (Session.LONDON, Session.LONDON_NY_OVERLAP, Session.NEW_YORK)
    max_spread_atr_multiple: Decimal = Decimal("0.12")
    news_blackout_impacts: tuple[str, ...] = ("high",)
    min_rr: Decimal = Decimal("1.5")


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "tdip"
    semver: str = "0.1.0"
    symbols: tuple[str, ...] = ("XAUUSD",)
    primary_timeframe: str = "M15"
    context_timeframes: tuple[str, ...] = ("H1", "H4", "D1")

    context: ContextConfig = ContextConfig()
    structure: StructureConfig = StructureConfig()
    liquidity: LiquidityConfig = LiquidityConfig()
    manipulation: ManipulationConfig = ManipulationConfig()
    setups: SetupsConfig = SetupsConfig()
    evidence_weights: dict[str, Decimal] = {
        "HTF_TREND_ALIGNMENT": Decimal("2.0"),
        "LIQUIDITY_SWEEP": Decimal("2.0"),
        "MANIPULATION_QUALITY": Decimal("2.0"),
        "STRUCTURE_BREAK": Decimal("2.0"),
        "RETEST_CONFIRMED": Decimal("1.5"),
        "CANDLE_CONFIRMATION": Decimal("1.0"),
        "MOMENTUM_EXPANSION": Decimal("1.5"),
    }
    confluence: ConfluenceConfig = ConfluenceConfig()
    trade_construction: TradeConstructionConfig = TradeConstructionConfig()
    filters: FiltersConfig = FiltersConfig()

    def config_sha256(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def label(self) -> str:
        return f"{self.name}@{self.semver}+{self.config_sha256()[:12]}"
