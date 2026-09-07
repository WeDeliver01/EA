"""SPEC-01 §3: Evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.market.enums import Direction
from app.domain.strategy.enums import EvidenceType


@dataclass(frozen=True, slots=True)
class Evidence:
    type: EvidenceType
    direction: Direction | None
    present: bool
    weight: Decimal  # from the strategy version config
    score: Decimal  # weight if present else 0
    detail: Mapping[str, Any] = field(default_factory=dict)  # JSON-safe, serialised to JSONB
