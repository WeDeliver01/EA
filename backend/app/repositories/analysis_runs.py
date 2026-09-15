"""SPEC-06 §5 step 5, P3: "every decision is recorded, including every
decision not to trade" - persists a `Decision` (WAIT or TRADE) plus its
full evidence and gate trail, in one flush.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.market.enums import Timeframe
from app.domain.strategy.decision import Decision
from app.models.tables import AnalysisEvidence, AnalysisGate, AnalysisRun, Instrument


@dataclass(frozen=True, slots=True)
class AnalysisRunSummary:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    symbol: str
    timeframe: str
    as_of: datetime
    mode: str
    regime: str
    outcome: str
    confluence_score: Decimal
    confluence_band: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    type: str
    direction: str | None
    present: bool
    weight: Decimal
    score: Decimal
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GateRecord:
    code: str
    passed: bool
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GateRejectionCount:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class GateRejectionSummary:
    since: datetime
    until: datetime
    total_evaluations: int
    trades: int
    rejections: tuple[GateRejectionCount, ...]


@dataclass(frozen=True, slots=True)
class AnalysisRunDetail:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    symbol: str
    strategy_version_id: UUID
    timeframe: str
    as_of: datetime
    mode: str
    regime: str
    outcome: str
    confluence_score: Decimal
    confluence_band: str
    direction: str | None
    entry: Decimal | None
    stop_loss: Decimal | None
    narrative: str
    engine_duration_ms: int
    market_snapshot: dict[str, Any]
    created_at: datetime
    evidence: tuple[EvidenceRecord, ...]
    gates: tuple[GateRecord, ...]


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

    async def list_runs(
        self,
        *,
        account_id: UUID | None = None,
        instrument_id: UUID | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> tuple[AnalysisRunSummary, ...]:
        """The decision journal - `strategy_worker`'s WAIT outcomes
        included, per P3: this is meant to answer "what has the bot been
        thinking," not just "what has it traded.\" """
        stmt = select(AnalysisRun, Instrument.canonical_symbol).join(
            Instrument, AnalysisRun.instrument_id == Instrument.id
        )
        if account_id is not None:
            stmt = stmt.where(AnalysisRun.account_id == account_id)
        if instrument_id is not None:
            stmt = stmt.where(AnalysisRun.instrument_id == instrument_id)
        if outcome is not None:
            stmt = stmt.where(AnalysisRun.outcome == outcome)
        if since is not None:
            stmt = stmt.where(AnalysisRun.as_of >= since)
        if until is not None:
            stmt = stmt.where(AnalysisRun.as_of <= until)
        stmt = stmt.order_by(AnalysisRun.as_of.desc()).limit(limit)

        result = await self._session.execute(stmt)
        return tuple(
            AnalysisRunSummary(
                id=row.id,
                account_id=row.account_id,
                instrument_id=row.instrument_id,
                symbol=symbol,
                timeframe=row.timeframe,
                as_of=row.as_of,
                mode=row.mode,
                regime=row.regime,
                outcome=row.outcome,
                confluence_score=row.confluence_score,
                confluence_band=row.confluence_band,
                created_at=row.created_at,
            )
            for row, symbol in result.all()
        )

    async def get_detail(self, analysis_run_id: UUID) -> AnalysisRunDetail | None:
        stmt = (
            select(AnalysisRun, Instrument.canonical_symbol)
            .join(Instrument, AnalysisRun.instrument_id == Instrument.id)
            .options(selectinload(AnalysisRun.evidence), selectinload(AnalysisRun.gates))
            .where(AnalysisRun.id == analysis_run_id)
        )
        result = await self._session.execute(stmt)
        row_pair = result.one_or_none()
        if row_pair is None:
            return None
        row, symbol = row_pair
        return AnalysisRunDetail(
            id=row.id,
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            symbol=symbol,
            strategy_version_id=row.strategy_version_id,
            timeframe=row.timeframe,
            as_of=row.as_of,
            mode=row.mode,
            regime=row.regime,
            outcome=row.outcome,
            confluence_score=row.confluence_score,
            confluence_band=row.confluence_band,
            direction=row.direction,
            entry=row.entry,
            stop_loss=row.stop_loss,
            narrative=row.narrative,
            engine_duration_ms=row.engine_duration_ms,
            market_snapshot=row.market_snapshot,
            created_at=row.created_at,
            evidence=tuple(
                EvidenceRecord(
                    type=e.type,
                    direction=e.direction,
                    present=e.present,
                    weight=e.weight,
                    score=e.score,
                    detail=e.detail,
                )
                for e in row.evidence
            ),
            gates=tuple(
                GateRecord(code=g.code, passed=g.passed, detail=g.detail) for g in row.gates
            ),
        )

    async def compute_gate_rejections(
        self,
        *,
        account_id: UUID | None,
        instrument_id: UUID | None,
        since: datetime,
        until: datetime,
    ) -> GateRejectionSummary:
        """SPEC-03 §8: answers "why has the bot not traded" in one query -
        counts per failing `GateCode` across every evaluation in the
        window, plus how many of those evaluations were `TRADE` at all."""
        base_filters = [AnalysisRun.as_of >= since, AnalysisRun.as_of <= until]
        if account_id is not None:
            base_filters.append(AnalysisRun.account_id == account_id)
        if instrument_id is not None:
            base_filters.append(AnalysisRun.instrument_id == instrument_id)

        total = (
            await self._session.execute(select(func.count(AnalysisRun.id)).where(*base_filters))
        ).scalar_one()
        trades = (
            await self._session.execute(
                select(func.count(AnalysisRun.id)).where(
                    *base_filters, AnalysisRun.outcome == "TRADE"
                )
            )
        ).scalar_one()

        rejections_stmt = (
            select(AnalysisGate.code, func.count(AnalysisGate.id))
            .join(AnalysisRun, AnalysisGate.analysis_run_id == AnalysisRun.id)
            .where(*base_filters, AnalysisGate.passed.is_(False))
            .group_by(AnalysisGate.code)
            .order_by(func.count(AnalysisGate.id).desc())
        )
        result = await self._session.execute(rejections_stmt)
        rejections = tuple(
            GateRejectionCount(code=code, count=count) for code, count in result.all()
        )

        return GateRejectionSummary(
            since=since, until=until, total_evaluations=total, trades=trades, rejections=rejections
        )
