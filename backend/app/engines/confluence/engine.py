"""SPEC-05 §3.6: ConfluenceEngine.

`score = sum(evidence.score) / sum(evidence.weight) * 10`, normalised to a
0..10 scale so adding an evidence type doesn't silently shift the threshold.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.strategy.evidence import Evidence
from app.engines.config import ConfluenceConfig


class ConfluenceEngine:
    def __init__(self, config: ConfluenceConfig) -> None:
        self._config = config

    def evaluate(self, evidence: tuple[Evidence, ...]) -> tuple[Decimal, str]:
        total_weight = sum((e.weight for e in evidence), Decimal(0))
        total_score = sum((e.score for e in evidence), Decimal(0))
        score = (total_score / total_weight * 10) if total_weight > 0 else Decimal(0)

        bands = self._config.bands
        if score >= bands.high:
            band = "HIGH"
        elif score >= bands.valid:
            band = "VALID"
        elif score >= bands.weak:
            band = "WEAK"
        else:
            band = "WAIT"
        return score, band
