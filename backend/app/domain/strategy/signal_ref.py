"""A lightweight reference to a recent signal, carried on MarketState so the
SetupDetector can suppress duplicate setups (SPEC-05 §3.5) without the engine
touching the database directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.market.enums import Direction


@dataclass(frozen=True, slots=True)
class SignalRef:
    id: UUID
    symbol: str
    setup_fingerprint: str
    direction: Direction
    created_at: datetime
