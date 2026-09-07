"""Session classification from a UTC timestamp.

Fixed UTC-hour windows, not DST-aware. Good enough for regime and session
filtering at MVP scope; a DST-correct calendar (accounting for US/UK clock
changes shifting session boundaries by an hour twice a year) is future work
- see docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.market.enums import Session

_SYDNEY = (21, 6)  # wraps midnight
_TOKYO = (0, 9)
_LONDON = (7, 16)
_NEW_YORK = (12, 21)
_OVERLAP = (12, 16)


def _in_window(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def classify_session(at: datetime) -> Session:
    if at.weekday() >= 5:  # Saturday, Sunday
        return Session.DEAD

    hour = at.hour
    if _in_window(hour, _OVERLAP):
        return Session.LONDON_NY_OVERLAP
    if _in_window(hour, _LONDON):
        return Session.LONDON
    if _in_window(hour, _NEW_YORK):
        return Session.NEW_YORK
    if _in_window(hour, _TOKYO):
        return Session.TOKYO
    if _in_window(hour, _SYDNEY):
        return Session.SYDNEY
    return Session.DEAD
