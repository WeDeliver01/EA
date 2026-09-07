"""SPEC-05 §3.1: ContextEngine.

Regime is computed as a decision table, evaluated top to bottom, first match
wins - not nested conditionals, so the rule that fired is always legible from
the code alone.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.market.bar import Bar
from app.domain.market.enums import Direction, Regime, Timeframe
from app.domain.market.market_state import MarketState
from app.engines.config import ContextConfig
from app.engines.context.sessions import classify_session
from app.engines.context.types import Context
from app.engines.indicators.adx import adx as compute_adx
from app.engines.indicators.atr import atr as compute_atr


def _bias_from_series(bars: tuple[Bar, ...], *, lookback: int) -> Direction | None:
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    sma = sum((b.close for b in window), Decimal(0)) / lookback
    last_close = bars[-1].close
    if last_close > sma:
        return Direction.LONG
    if last_close < sma:
        return Direction.SHORT
    return None


def _percentile_rank(values: list[Decimal], current: Decimal) -> Decimal:
    if not values:
        return Decimal(0)
    count = sum(1 for v in values if v <= current)
    return Decimal(count) / Decimal(len(values)) * 100


def _bars_since_session_open(bars: tuple[Bar, ...], as_of_session: str) -> int:
    count = 0
    for bar in reversed(bars):
        if classify_session(bar.open_time).value != as_of_session:
            break
        count += 1
    return count


class ContextEngine:
    def __init__(self, config: ContextConfig) -> None:
        self._config = config

    def evaluate(self, state: MarketState) -> Context:
        cfg = self._config
        primary_bars = state.bars.get(state.primary_tf, ())

        atr_by_tf: dict[Timeframe, Decimal] = {}
        for tf, bars in state.bars.items():
            series = compute_atr(bars, period=cfg.atr_period)
            last = series[-1] if series else None
            if last is not None:
                atr_by_tf[tf] = last

        primary_atr_series = compute_atr(primary_bars, period=cfg.atr_period)
        primary_atr_values = [v for v in primary_atr_series if v is not None]

        insufficient_data = (
            len(primary_bars) < max(cfg.atr_period, cfg.adx_period) * 2
            or not primary_atr_values
            or state.primary_tf not in atr_by_tf
        )

        if insufficient_data:
            return Context(
                regime=Regime.UNKNOWN,
                htf_bias=None,
                itf_bias=None,
                atr=atr_by_tf,
                atr_percentile=Decimal(0),
                atr_expanding=False,
                adx=Decimal(0),
                range_pct_of_atr=Decimal(0),
                session=state.session,
                bars_since_session_open=0,
                inputs={"insufficient_data": True},
            )

        current_atr = atr_by_tf[state.primary_tf]
        lookback_values = primary_atr_values[-cfg.atr_percentile_lookback :]
        atr_percentile = _percentile_rank(lookback_values, current_atr)
        atr_expanding = (
            len(primary_atr_values) >= 2 and primary_atr_values[-1] > primary_atr_values[-2]
        )

        adx_series = compute_adx(primary_bars, period=cfg.adx_period)
        adx_value = next((v for v in reversed(adx_series) if v is not None), Decimal(0))

        range_pct_of_atr = (
            (primary_bars[-1].range / current_atr * 100) if current_atr != 0 else Decimal(0)
        )

        # H4 stands in for "higher timeframe", H1 for "intermediate timeframe" -
        # the two names SPEC-05 uses without pinning them to specific
        # Timeframe members. Falls back to whichever context timeframe is
        # available if H4/H1 weren't supplied.
        htf_bars = state.bars.get(Timeframe.H4) or state.bars.get(Timeframe.D1) or ()
        itf_bars = state.bars.get(Timeframe.H1) or ()
        htf_bias = _bias_from_series(htf_bars, lookback=20)
        itf_bias = _bias_from_series(itf_bars, lookback=20)

        regime = self._classify_regime(
            atr_percentile=atr_percentile, adx=adx_value, htf_bias=htf_bias, itf_bias=itf_bias
        )

        session_value = state.session.value
        bars_since_open = _bars_since_session_open(primary_bars, session_value)

        return Context(
            regime=regime,
            htf_bias=htf_bias,
            itf_bias=itf_bias,
            atr=atr_by_tf,
            atr_percentile=atr_percentile,
            atr_expanding=atr_expanding,
            adx=adx_value,
            range_pct_of_atr=range_pct_of_atr,
            session=state.session,
            bars_since_session_open=bars_since_open,
            inputs={
                "atr_period": cfg.atr_period,
                "adx_period": cfg.adx_period,
                "adx_trend_threshold": str(cfg.adx_trend_threshold),
                "compression_atr_percentile": str(cfg.compression_atr_percentile),
                "expansion_atr_percentile": str(cfg.expansion_atr_percentile),
            },
        )

    def _classify_regime(
        self,
        *,
        atr_percentile: Decimal,
        adx: Decimal,
        htf_bias: Direction | None,
        itf_bias: Direction | None,
    ) -> Regime:
        cfg = self._config
        low_adx = adx < cfg.adx_trend_threshold
        aligned = htf_bias is not None and htf_bias == itf_bias
        conflicting = htf_bias is not None and itf_bias is not None and htf_bias != itf_bias

        if atr_percentile < cfg.compression_atr_percentile and low_adx:
            return Regime.COMPRESSION
        if not low_adx and aligned:
            return Regime.TRENDING_UP if htf_bias == Direction.LONG else Regime.TRENDING_DOWN
        if atr_percentile > cfg.expansion_atr_percentile:
            return Regime.EXPANSION
        if (
            cfg.compression_atr_percentile <= atr_percentile <= cfg.expansion_atr_percentile
            and low_adx
        ):
            return Regime.RANGING
        if conflicting:
            return Regime.CHOPPY
        return Regime.UNKNOWN
