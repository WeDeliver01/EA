"""SPEC-07 §3 / SPEC-10 Phase 3 acceptance tests for data quality checks.

Each check is exercised at its exact pass/fail boundary, per the acceptance
criterion that promotion-adjacent gate logic be tested at the threshold, not
just with obviously-good or obviously-bad data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market.bar import Bar
from app.domain.market.enums import AssetClass, Timeframe
from app.research.data_quality import DataQualityReport, QualityCheck, check_data_quality

pytestmark = pytest.mark.unit

_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # a Monday


def _bar(
    t: datetime,
    *,
    tf: Timeframe = Timeframe.M15,
    symbol: str = "TEST",
    volume: int = 100,
    spread: int | None = 1,
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=tf,
        open_time=t,
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100.2"),
        tick_volume=volume,
        real_volume=None,
        spread_points=spread,
    )


def _contiguous_bars(
    count: int,
    *,
    start: datetime = _START,
    tf: Timeframe = Timeframe.M15,
    skip_indices: frozenset[int] = frozenset(),
    zero_volume_indices: frozenset[int] = frozenset(),
    spread: int | None = 1,
) -> tuple[Bar, ...]:
    bars = []
    for i in range(count):
        if i in skip_indices:
            continue
        t = start + timedelta(seconds=tf.seconds * i)
        volume = 0 if i in zero_volume_indices else 100
        bars.append(_bar(t, tf=tf, volume=volume, spread=spread))
    return tuple(bars)


def _check(report: DataQualityReport, name: str) -> QualityCheck:
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"no check named {name!r} in report"
    return matches[0]


def test_empty_bars_fails_non_empty_check() -> None:
    report = check_data_quality((), timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    assert report.passed is False
    assert _check(report, "non_empty").passed is False


def test_no_duplicate_timestamps_passes() -> None:
    bars = _contiguous_bars(10)
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    assert _check(report, "duplicate_timestamps").passed is True


def test_duplicate_timestamps_detected() -> None:
    bars = _contiguous_bars(10)
    duplicate = _bar(bars[3].open_time)
    report = check_data_quality(
        (*bars, duplicate), timeframe=Timeframe.M15, asset_class=AssetClass.INDEX
    )
    check = _check(report, "duplicate_timestamps")
    assert check.passed is False
    assert check.detail["count"] == 1


def test_ohlc_sane_always_passes_for_structurally_valid_bars() -> None:
    """`Bar.__post_init__` refuses to construct an invalid bar, so this check
    can never fail for real `Bar` instances - it stays in the report as
    evidence, not because it can trigger."""
    bars = _contiguous_bars(5)
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "ohlc_sane")
    assert check.passed is True
    assert check.detail["count"] == 0


def test_weekend_bars_flagged_for_fx() -> None:
    saturday = _START + timedelta(days=(5 - _START.weekday()))
    assert saturday.weekday() == 5
    bars = (_bar(_START), _bar(saturday))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.FX)
    check = _check(report, "no_weekend_bars")
    assert check.passed is False
    assert check.detail["count"] == 1


def test_no_weekend_bars_check_omitted_for_non_fx_metal_asset_classes() -> None:
    saturday = _START + timedelta(days=(5 - _START.weekday()))
    bars = (_bar(_START), _bar(saturday))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    assert not [c for c in report.checks if c.name == "no_weekend_bars"]


def test_gap_exactly_at_four_hour_threshold_passes() -> None:
    bar0 = _bar(_START, tf=Timeframe.M1)
    bar1 = _bar(bar0.close_time + timedelta(hours=4), tf=Timeframe.M1)
    report = check_data_quality((bar0, bar1), timeframe=Timeframe.M1, asset_class=AssetClass.INDEX)
    check = _check(report, "largest_gap_under_threshold")
    assert check.passed is True
    assert check.detail["largest_gap_hours"] == pytest.approx(4.0)


def test_gap_one_second_over_four_hour_threshold_fails() -> None:
    bar0 = _bar(_START, tf=Timeframe.M1)
    bar1 = _bar(bar0.close_time + timedelta(hours=4, seconds=1), tf=Timeframe.M1)
    report = check_data_quality((bar0, bar1), timeframe=Timeframe.M1, asset_class=AssetClass.INDEX)
    check = _check(report, "largest_gap_under_threshold")
    assert check.passed is False


def test_weekend_spanning_gap_excluded_for_fx() -> None:
    friday = datetime(2026, 1, 9, 21, 0, tzinfo=UTC)  # Friday
    assert friday.weekday() == 4
    monday = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)  # Monday, >4h after friday close
    assert monday.weekday() == 0
    bar0 = _bar(friday, tf=Timeframe.M15)
    bar1 = _bar(monday, tf=Timeframe.M15)
    report = check_data_quality((bar0, bar1), timeframe=Timeframe.M15, asset_class=AssetClass.FX)
    check = _check(report, "largest_gap_under_threshold")
    assert check.passed is True
    assert check.detail["largest_gap_hours"] == 0.0


def test_same_gap_not_excluded_for_non_fx_metal_asset_class() -> None:
    friday = datetime(2026, 1, 9, 21, 0, tzinfo=UTC)
    monday = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)
    bar0 = _bar(friday, tf=Timeframe.M15)
    bar1 = _bar(monday, tf=Timeframe.M15)
    report = check_data_quality((bar0, bar1), timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "largest_gap_under_threshold")
    assert check.passed is False


def test_missing_bars_at_exactly_half_percent_fails() -> None:
    # 200 expected slots, one skipped mid-sequence -> missing_pct == 0.005
    # exactly, which the strict `<` comparison must reject.
    bars = _contiguous_bars(200, skip_indices=frozenset({100}))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "missing_bars_under_threshold")
    assert check.detail["expected_bars"] == 200
    assert check.passed is False


def test_missing_bars_just_under_half_percent_passes() -> None:
    # 201 expected slots, one skipped mid-sequence -> missing_pct ~= 0.004975,
    # just under the 0.005 threshold.
    bars = _contiguous_bars(201, skip_indices=frozenset({100}))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "missing_bars_under_threshold")
    assert check.detail["expected_bars"] == 201
    assert check.passed is True


def test_zero_volume_bars_at_exactly_two_percent_fails() -> None:
    bars = _contiguous_bars(100, zero_volume_indices=frozenset({10, 20}))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "zero_volume_bars_under_threshold")
    assert check.detail["zero_volume_pct"] == pytest.approx(0.02)
    assert check.passed is False


def test_zero_volume_bars_just_under_two_percent_passes() -> None:
    bars = _contiguous_bars(101, zero_volume_indices=frozenset({10, 20}))
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    check = _check(report, "zero_volume_bars_under_threshold")
    assert check.passed is True


def test_spread_data_present_check_omitted_when_not_required() -> None:
    bars = _contiguous_bars(5, spread=None)
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    assert not [c for c in report.checks if c.name == "spread_data_present"]


def test_spread_data_present_fails_when_required_and_missing() -> None:
    bars = _contiguous_bars(5, spread=None)
    report = check_data_quality(
        bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX, require_spread=True
    )
    check = _check(report, "spread_data_present")
    assert check.passed is False
    assert check.detail["missing_count"] == 5


def test_spread_data_present_passes_when_required_and_present() -> None:
    bars = _contiguous_bars(5, spread=1)
    report = check_data_quality(
        bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX, require_spread=True
    )
    check = _check(report, "spread_data_present")
    assert check.passed is True


def test_report_passed_is_true_only_when_every_check_passes() -> None:
    bars = _contiguous_bars(200)
    report = check_data_quality(bars, timeframe=Timeframe.M15, asset_class=AssetClass.INDEX)
    assert report.passed is True
    assert report.failures == ()

    duplicate = _bar(bars[0].open_time)
    bad_report = check_data_quality(
        (*bars, duplicate), timeframe=Timeframe.M15, asset_class=AssetClass.INDEX
    )
    assert bad_report.passed is False
    assert "duplicate_timestamps" in {c.name for c in bad_report.failures}
