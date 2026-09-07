"""SPEC-07 §3: data quality gates.

`Bar.__post_init__` already refuses to construct a bar with `high < low` or
`open`/`close` outside `[low, high]` (`app/domain/market/bar.py`), so that
check here can never fail for any sequence of real `Bar` objects - it stays
in the report because the *absence* of a failure is itself useful evidence,
not because it can realistically trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise

from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Timeframe

_MAX_MISSING_BARS_PCT = 0.005
_MAX_GAP_HOURS = 4
_MAX_ZERO_VOLUME_PCT = 0.02


@dataclass(frozen=True, slots=True)
class QualityCheck:
    name: str
    passed: bool
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    checks: tuple[QualityCheck, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[QualityCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)


def _is_weekend(bar: Bar) -> bool:
    return bar.open_time.weekday() >= 5


def check_data_quality(
    bars: tuple[Bar, ...],
    *,
    timeframe: Timeframe,
    asset_class: AssetClass,
    require_spread: bool = False,
) -> DataQualityReport:
    checks: list[QualityCheck] = []

    if not bars:
        return DataQualityReport(
            checks=(QualityCheck("non_empty", passed=False, detail={"bar_count": 0}),)
        )

    sorted_bars = sorted(bars, key=lambda b: b.open_time)

    # Duplicate timestamps.
    open_times = [b.open_time for b in sorted_bars]
    duplicate_count = len(open_times) - len(set(open_times))
    checks.append(
        QualityCheck(
            "duplicate_timestamps", passed=duplicate_count == 0, detail={"count": duplicate_count}
        )
    )

    # OHLC sanity - structurally guaranteed by Bar itself; kept as evidence.
    bad_ohlc = sum(1 for b in sorted_bars if b.low > b.high or not (b.low <= b.open <= b.high))
    checks.append(QualityCheck("ohlc_sane", passed=bad_ohlc == 0, detail={"count": bad_ohlc}))

    # Weekend bars, for asset classes that don't trade weekends.
    if asset_class in (AssetClass.FX, AssetClass.METAL):
        weekend_count = sum(1 for b in sorted_bars if _is_weekend(b))
        checks.append(
            QualityCheck(
                "no_weekend_bars", passed=weekend_count == 0, detail={"count": weekend_count}
            )
        )

    # Gaps and missing-bar estimate: a gap spanning a weekend (an FX market
    # closing Friday evening and reopening Sunday evening) is an excused
    # closure, not a data gap - it is subtracted out of the expected bar
    # count so it neither trips the gap check nor registers as missing bars.
    # Any other gap is a real anomaly: it must NOT be subtracted out, or it
    # would silently explain away the very bars it caused to be missing.
    tf_seconds = timeframe.seconds
    largest_gap = timedelta(0)
    excused_gap_seconds = 0.0
    for prev, curr in pairwise(sorted_bars):
        gap = curr.open_time - prev.close_time
        if gap <= timedelta(0):
            continue
        spans_weekend = prev.open_time.weekday() >= 4 and curr.open_time.weekday() <= 1
        if spans_weekend and asset_class in (AssetClass.FX, AssetClass.METAL):
            excused_gap_seconds += gap.total_seconds()
            continue
        largest_gap = max(largest_gap, gap)

    checks.append(
        QualityCheck(
            "largest_gap_under_threshold",
            passed=largest_gap <= timedelta(hours=_MAX_GAP_HOURS),
            detail={"largest_gap_hours": largest_gap.total_seconds() / 3600},
        )
    )

    span_seconds = (sorted_bars[-1].close_time - sorted_bars[0].open_time).total_seconds()
    expected_bars = max(1, round((span_seconds - excused_gap_seconds) / tf_seconds))
    missing = max(0, expected_bars - len(sorted_bars))
    missing_pct = missing / expected_bars
    checks.append(
        QualityCheck(
            "missing_bars_under_threshold",
            passed=missing_pct < _MAX_MISSING_BARS_PCT,
            detail={
                "missing_pct": missing_pct,
                "expected_bars": expected_bars,
                "actual_bars": len(sorted_bars),
            },
        )
    )

    zero_volume_count = sum(1 for b in sorted_bars if b.tick_volume == 0)
    zero_volume_pct = zero_volume_count / len(sorted_bars)
    checks.append(
        QualityCheck(
            "zero_volume_bars_under_threshold",
            passed=zero_volume_pct < _MAX_ZERO_VOLUME_PCT,
            detail={"zero_volume_pct": zero_volume_pct},
        )
    )

    if require_spread:
        missing_spread = sum(1 for b in sorted_bars if b.spread_points is None)
        checks.append(
            QualityCheck(
                "spread_data_present",
                passed=missing_spread == 0,
                detail={"missing_count": missing_spread},
            )
        )

    return DataQualityReport(checks=tuple(checks))
