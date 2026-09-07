from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.market.enums import Session
from app.engines.context.sessions import classify_session

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "hour,expected",
    [
        (13, Session.LONDON_NY_OVERLAP),
        (8, Session.LONDON),
        (18, Session.NEW_YORK),
        (2, Session.TOKYO),
        (22, Session.SYDNEY),
    ],
)
def test_classify_session_by_utc_hour(hour: int, expected: Session) -> None:
    at = datetime(2026, 9, 7, hour, 0, tzinfo=UTC)  # a Monday
    assert classify_session(at) == expected


def test_classify_session_is_dead_on_weekends() -> None:
    saturday = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    sunday = datetime(2026, 9, 6, 13, 0, tzinfo=UTC)
    assert classify_session(saturday) == Session.DEAD
    assert classify_session(sunday) == Session.DEAD
