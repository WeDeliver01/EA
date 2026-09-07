"""Calendar (news) event, mirroring the `calendar_events` table (SPEC-02 §3).

Used by MarketState.calendar_events (only events within the blackout window
are included, per SPEC-01 §2) and the NEWS_BLACKOUT gate (SPEC-06 §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CalendarImpact(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_time: datetime
    currency: str
    impact: CalendarImpact
    title: str
