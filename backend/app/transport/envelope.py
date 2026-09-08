"""SPEC-04 §3 message envelope - the backend's mirror of `agent/transport.py`'s
`Envelope`. The two processes agree on this JSON shape as a protocol, not a
shared Python dependency (the agent is Windows-only and doesn't import
`backend.app`), so this is kept in sync by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Envelope:
    v: int
    type: str
    id: str
    correlation_id: str | None
    ts: str
    payload: dict[str, Any]

    @classmethod
    def decode(cls, raw: str | bytes) -> Envelope:
        data = json.loads(raw)
        return cls(
            v=data["v"],
            type=data["type"],
            id=data["id"],
            correlation_id=data.get("correlation_id"),
            ts=data["ts"],
            payload=data.get("payload", {}),
        )

    def encode(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "type": self.type,
                "id": self.id,
                "correlation_id": self.correlation_id,
                "ts": self.ts,
                "payload": self.payload,
            }
        )
