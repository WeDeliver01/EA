"""Clock protocol. Domain and engine code never read the wall clock directly (P7).

Every caller that needs "now" takes a ``Clock`` and calls ``clock.now()``.
Production code uses ``SystemClock``; tests and the backtester use
``FrozenClock`` so evaluations are reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that returns a fixed instant, or can be advanced explicitly."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(seconds=seconds)
