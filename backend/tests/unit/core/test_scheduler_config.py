"""`SchedulerConfig.from_settings()` is the one thing standing between a
default `.env` (nothing configured) and an autonomously trading process -
worth its own focused tests: unconfigured must stay `None`, and a fully
configured settings object must parse into exactly what was given."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.market.enums import Timeframe
from app.services.scheduler import SchedulerConfig

pytestmark = pytest.mark.unit


def test_unconfigured_settings_yields_no_scheduler() -> None:
    settings = Settings()
    assert SchedulerConfig.from_settings(settings) is None


def test_partially_configured_settings_yields_no_scheduler() -> None:
    settings = Settings(scheduler_account_id=uuid4())  # instrument_id/strategy_version_id unset
    assert SchedulerConfig.from_settings(settings) is None


def test_fully_configured_settings_parses_correctly() -> None:
    account_id = uuid4()
    instrument_id = uuid4()
    strategy_version_id = uuid4()
    settings = Settings(
        scheduler_account_id=account_id,
        scheduler_instrument_id=instrument_id,
        scheduler_strategy_version_id=strategy_version_id,
        scheduler_symbol="XAUUSD",
        scheduler_environment="demo",
        scheduler_primary_timeframe="M15",
        scheduler_context_timeframes="H1,H4,D1",
        scheduler_scan_interval_seconds=7.0,
        scheduler_dispatch_interval_seconds=3.0,
        reconciliation_interval_seconds=45,
        candle_close_grace_ms=2000,
        global_trading_enabled=True,
    )

    config = SchedulerConfig.from_settings(settings)

    assert config is not None
    assert config.account_id == account_id
    assert config.instrument_id == instrument_id
    assert config.strategy_version_id == strategy_version_id
    assert config.symbol == "XAUUSD"
    assert config.environment == "demo"
    assert config.primary_tf == Timeframe.M15
    assert config.context_timeframes == (Timeframe.H1, Timeframe.H4, Timeframe.D1)
    assert config.scan_interval_seconds == 7.0
    assert config.outbox_dispatch_interval_seconds == 3.0
    assert config.reconciliation_interval_seconds == 45.0
    assert config.candle_close_grace_ms == 2000
    assert config.global_trading_enabled is True


def test_context_timeframes_handles_whitespace_and_trailing_comma() -> None:
    settings = Settings(
        scheduler_account_id=uuid4(),
        scheduler_instrument_id=uuid4(),
        scheduler_strategy_version_id=uuid4(),
        scheduler_symbol="XAUUSD",
        scheduler_context_timeframes=" H1 , H4, ",
    )

    config = SchedulerConfig.from_settings(settings)

    assert config is not None
    assert config.context_timeframes == (Timeframe.H1, Timeframe.H4)
