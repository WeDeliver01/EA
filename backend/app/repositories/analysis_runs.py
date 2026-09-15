"""SPEC-06 §5 step 5, P3: "every decision is recorded, including every
decision not to trade" - persists a `Decision` (WAIT or TRADE) plus its
full evidence and gate trail, in one flush.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market.enums import Timeframe
from app.domain.strategy.decision import Decision
from app.models.tables import AnalysisEvidence, AnalysisGate, AnalysisRun


class AnalysisRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_from_decision(
        self,
        decision: Decision,
        *,
        account_id: UUID,
        instrument_id: UUID,
        timeframe: Timeframe,
        mode: str,
        created_at: datetime,
    ) -> UUID:
        row = AnalysisRun(
            id=uuid4(),
            account_id=account_id,
            instrument_id=instrument_id,
            strategy_version_id=decision.strategy_version_id,
            timeframe=timeframe.value,
            as_of=decision.as_of,
            mode=mode,
            regime=decision.regime.value,
            outcome=decision.outcome.value,
            confluence_score=decision.confluence_score,
            confluence_band=decision.confluence_band,
            direction=decision.direction.value if decision.direction is not None else None,
            setup_kind=decision.setup.kind if decision.setup is not None else None,
            setup_fingerprint=(decision.setup.fingerprint if decision.setup is not None else None),
            entry=decision.entry,
            stop_loss=decision.stop_loss,
            narrative=decision.narrative,
            engine_duration_ms=decision.engine_duration_ms,
            # No live MarketState serialisation exists yet - a telemetry
            # gap (nothing needs it to function correctly today), not a
            # correctness one; matches the same placeholder the earlier
            # /agent/test-trade demo endpoint used.
            market_snapshot={},
            created_at=created_at,
        )
        self._session.add(row)
        await self._session.flush()

        self._session.add_all(
            AnalysisEvidence(
                analysis_run_id=row.id,
                type=e.type.value,
                direction=e.direction.value if e.direction is not None else None,
                present=e.present,
                weight=e.weight,
                score=e.score,
                detail=dict(e.detail),
            )
            for e in decision.evidence
        )
        self._session.add_all(
            AnalysisGate(
                analysis_run_id=row.id,
                code=g.code.value,
                passed=g.passed,
                detail=dict(g.detail),
            )
            for g in decision.gates
        )
        await self._session.flush()
        return row.id
