"""SPEC-01 §3: GateResult."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.strategy.enums import GateCode


@dataclass(frozen=True, slots=True)
class GateResult:
    code: GateCode
    passed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)
