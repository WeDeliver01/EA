"""SPEC-04 §3. This must decode exactly what `agent/transport.py`'s
`Envelope.encode()` produces and vice versa - two independent codebases
agreeing on one wire shape."""

from __future__ import annotations

import pytest

from app.transport.envelope import Envelope

pytestmark = pytest.mark.unit


def test_round_trips() -> None:
    envelope = Envelope(
        v=1,
        type="command.place_order",
        id="01JCXG4Q7T8N2M9RZKWDVYE3PA",
        correlation_id=None,
        ts="2026-09-07T09:14:22.318Z",
        payload={"symbol": "XAUUSD", "volume": "0.10"},
    )
    decoded = Envelope.decode(envelope.encode())
    assert decoded == envelope


def test_decode_defaults_missing_payload_to_empty_dict() -> None:
    raw = '{"v": 1, "type": "event.heartbeat", "id": "abc", "ts": "2026-01-01T00:00:00Z"}'
    envelope = Envelope.decode(raw)
    assert envelope.payload == {}
    assert envelope.correlation_id is None


def test_decode_reads_agent_formatted_json() -> None:
    """Exact shape `agent/transport.py`'s `Envelope.encode()` produces."""
    raw = (
        '{"v": 1, "type": "event.place_order_result", "id": "evt-1", '
        '"correlation_id": "cmd-1", "ts": "2026-09-07T09:14:22.318Z", '
        '"payload": {"retcode": 10009}}'
    )
    envelope = Envelope.decode(raw)
    assert envelope.type == "event.place_order_result"
    assert envelope.correlation_id == "cmd-1"
    assert envelope.payload == {"retcode": 10009}
