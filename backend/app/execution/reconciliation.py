"""SPEC-06 §6: reconciliation.

`classify()` is the pure comparison (given a local view and a broker view,
what's wrong) so the nine-row table in SPEC-06 §6 is testable without a
database or a broker. `Reconciler` is the DB/broker-touching orchestration
around it, applying only the automatic resolutions SPEC-06 §6 explicitly
permits - everything else stays unresolved, which is what activates the
`RECONCILIATION_UNRESOLVED` gate (`app.execution.dispatcher`).

Scope for this MVP pass (documented in docs/adr/0001-mvp-scope.md):
`DUPLICATE_POSITION` and `BALANCE_MISMATCH` are not classified - both need
correlation data (a setup fingerprint history, a broker-reported balance
feed) this MVP doesn't carry. `ORPHAN_INTENT`'s broker-history search is a
single pass, not the spec's "search, retry, expire after 3 attempts" - the
retry bookkeeping needs a place to persist attempt counts across
reconciliation runs that doesn't exist yet.

**The system never automatically closes a position it does not
recognise** (SPEC-06 §6) - `UNKNOWN_AT_BROKER` with no matching intent
creates an `ORPHANED` position and raises critical; it is never closed by
this module, under any code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.enums import DealType, ExecutionState, OrderSide, PositionStatus
from app.domain.market.enums import Direction
from app.execution.broker import AsyncBroker, BrokerPositionSnapshot
from app.execution.event_consumer import EventConsumer
from app.models.tables import PositionRow
from app.repositories.accounts import AccountRepository
from app.repositories.positions import PositionRepository
from app.repositories.reconciliation import ReconciliationRepository
from app.repositories.trade_intents import TradeIntentRepository

_ORPHAN_INTENT_GRACE = timedelta(seconds=120)


@dataclass(frozen=True, slots=True)
class LocalOpenPosition:
    id: UUID
    broker_position_id: str
    volume: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    entry_price: Decimal


@dataclass(frozen=True, slots=True)
class PendingIntent:
    id: UUID
    client_order_id: str
    magic: int
    symbol: str
    side: OrderSide
    volume: Decimal
    created_at: datetime
    state: ExecutionState  # SENT or UNKNOWN


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    severity: str  # "critical" | "warning"
    local_state: dict[str, object] | None
    broker_state: dict[str, object] | None
    local_position_id: UUID | None = None
    broker_position_id: str | None = None
    matched_intent_id: UUID | None = None


def _position_dict(position: LocalOpenPosition) -> dict[str, object]:
    return {
        "broker_position_id": position.broker_position_id,
        "volume": str(position.volume),
        "stop_loss": str(position.stop_loss) if position.stop_loss is not None else None,
        "take_profit": str(position.take_profit) if position.take_profit is not None else None,
        "entry_price": str(position.entry_price),
    }


def _broker_dict(position: BrokerPositionSnapshot) -> dict[str, object]:
    return {
        "broker_position_id": position.broker_position_id,
        "volume": str(position.volume),
        "stop_loss": str(position.stop_loss) if position.stop_loss is not None else None,
        "take_profit": str(position.take_profit) if position.take_profit is not None else None,
        "entry_price": str(position.entry_price),
        "magic": position.magic,
        "side": position.side.value,
    }


def classify(
    *,
    local_positions: list[LocalOpenPosition],
    broker_positions: tuple[BrokerPositionSnapshot, ...],
    pending_intents: list[PendingIntent],
    as_of: datetime,
    sl_tp_tolerance: Decimal = Decimal("0.01"),
) -> list[Finding]:
    findings: list[Finding] = []
    broker_by_id = {p.broker_position_id: p for p in broker_positions}
    local_ids = {p.broker_position_id for p in local_positions}

    for local in local_positions:
        broker_pos = broker_by_id.get(local.broker_position_id)
        if broker_pos is None:
            findings.append(
                Finding(
                    kind="MISSING_AT_BROKER",
                    severity="critical",
                    local_state=_position_dict(local),
                    broker_state=None,
                    local_position_id=local.id,
                    broker_position_id=local.broker_position_id,
                )
            )
            continue

        if local.volume != broker_pos.volume:
            findings.append(
                Finding(
                    kind="VOLUME_MISMATCH",
                    severity="critical",
                    local_state=_position_dict(local),
                    broker_state=_broker_dict(broker_pos),
                    local_position_id=local.id,
                    broker_position_id=local.broker_position_id,
                )
            )

        sl_differs = (local.stop_loss is None) != (broker_pos.stop_loss is None) or (
            local.stop_loss is not None
            and broker_pos.stop_loss is not None
            and abs(local.stop_loss - broker_pos.stop_loss) > sl_tp_tolerance
        )
        if sl_differs:
            findings.append(
                Finding(
                    kind="SL_MISMATCH",
                    severity="warning",
                    local_state=_position_dict(local),
                    broker_state=_broker_dict(broker_pos),
                    local_position_id=local.id,
                    broker_position_id=local.broker_position_id,
                )
            )

        if (
            local.take_profit is not None
            and broker_pos.take_profit is not None
            and abs(local.take_profit - broker_pos.take_profit) > sl_tp_tolerance
        ):
            findings.append(
                Finding(
                    kind="TP_MISMATCH",
                    severity="warning",
                    local_state=_position_dict(local),
                    broker_state=_broker_dict(broker_pos),
                    local_position_id=local.id,
                    broker_position_id=local.broker_position_id,
                )
            )

        if abs(local.entry_price - broker_pos.entry_price) > sl_tp_tolerance:
            findings.append(
                Finding(
                    kind="PRICE_MISMATCH",
                    severity="warning",
                    local_state=_position_dict(local),
                    broker_state=_broker_dict(broker_pos),
                    local_position_id=local.id,
                    broker_position_id=local.broker_position_id,
                )
            )

    for broker_pos in broker_positions:
        if broker_pos.broker_position_id in local_ids:
            continue
        matched = next((i for i in pending_intents if i.magic == broker_pos.magic), None)
        findings.append(
            Finding(
                kind="UNKNOWN_AT_BROKER",
                severity="critical",
                local_state=None,
                broker_state=_broker_dict(broker_pos),
                broker_position_id=broker_pos.broker_position_id,
                matched_intent_id=matched.id if matched is not None else None,
            )
        )

    for intent in pending_intents:
        if as_of - intent.created_at < _ORPHAN_INTENT_GRACE:
            # Every normal order is briefly SENT with no broker position
            # yet, between the outbox commit and the dispatcher actually
            # running - that is not an anomaly, so it isn't flagged until
            # it has had time to resolve on its own (SPEC-06 §6).
            continue
        has_broker_position = any(bp.magic == intent.magic for bp in broker_positions)
        if not has_broker_position:
            findings.append(
                Finding(
                    kind="ORPHAN_INTENT",
                    severity="critical",
                    local_state={
                        "trade_intent_id": str(intent.id),
                        "client_order_id": intent.client_order_id,
                        "state": intent.state.value,
                    },
                    broker_state=None,
                    matched_intent_id=intent.id,
                )
            )

    return findings


class Reconciler:
    def __init__(
        self,
        *,
        session: AsyncSession,
        broker: AsyncBroker,
        reconciliation_repo: ReconciliationRepository,
        intent_repo: TradeIntentRepository,
        position_repo: PositionRepository,
        event_consumer: EventConsumer,
        account_repo: AccountRepository,
    ) -> None:
        self._session = session
        self._broker = broker
        self._reconciliation_repo = reconciliation_repo
        self._intent_repo = intent_repo
        self._position_repo = position_repo
        self._event_consumer = event_consumer
        self._account_repo = account_repo

    async def run(
        self,
        *,
        account_id: UUID,
        instrument_id: UUID,
        local_positions: list[LocalOpenPosition],
        pending_intents: list[PendingIntent],
        as_of: datetime,
    ) -> list[Finding]:
        broker_positions = await self._broker.get_positions()
        findings = classify(
            local_positions=local_positions,
            broker_positions=broker_positions,
            pending_intents=pending_intents,
            as_of=as_of,
        )

        run_id = await self._reconciliation_repo.start_run(account_id, started_at=as_of)
        for finding in findings:
            await self._apply_resolution(
                finding,
                run_id=run_id,
                account_id=account_id,
                instrument_id=instrument_id,
                as_of=as_of,
            )
        await self._reconciliation_repo.finish_run(
            run_id,
            finished_at=as_of,
            status="clean" if not findings else "discrepancies",
            local_position_count=len(local_positions),
            broker_position_count=len(broker_positions),
            discrepancy_count=len(findings),
        )
        return findings

    async def _apply_resolution(
        self,
        finding: Finding,
        *,
        run_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        as_of: datetime,
    ) -> None:
        if finding.kind == "MISSING_AT_BROKER":
            await self._resolve_missing_at_broker(
                finding,
                run_id=run_id,
                account_id=account_id,
                instrument_id=instrument_id,
                as_of=as_of,
            )
            return

        if finding.kind == "UNKNOWN_AT_BROKER":
            await self._resolve_unknown_at_broker(
                finding,
                run_id=run_id,
                account_id=account_id,
                instrument_id=instrument_id,
                as_of=as_of,
            )
            return

        if finding.kind == "SL_MISMATCH":
            await self._resolve_sl_mismatch(
                finding, run_id=run_id, account_id=account_id, as_of=as_of
            )
            return

        await self._reconciliation_repo.record_discrepancy(
            reconciliation_run_id=run_id,
            account_id=account_id,
            kind=finding.kind,
            severity=finding.severity,
            local_state=finding.local_state,
            broker_state=finding.broker_state,
            created_at=as_of,
        )

    async def _resolve_missing_at_broker(
        self,
        finding: Finding,
        *,
        run_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        as_of: datetime,
    ) -> None:
        assert finding.broker_position_id is not None
        deals = await self._broker.get_deals()
        matching_deal = next(
            (
                d
                for d in deals
                if d.broker_position_id == finding.broker_position_id
                and d.deal_type in (DealType.EXIT, DealType.PARTIAL_EXIT)
            ),
            None,
        )
        discrepancy_id = await self._reconciliation_repo.record_discrepancy(
            reconciliation_run_id=run_id,
            account_id=account_id,
            kind=finding.kind,
            severity=finding.severity,
            local_state=finding.local_state,
            broker_state=finding.broker_state,
            created_at=as_of,
        )
        if matching_deal is None:
            return  # stays unresolved: no evidence yet of how it closed

        await self._event_consumer.record_deal(
            matching_deal, account_id=account_id, instrument_id=instrument_id, trade_intent_id=None
        )
        if matching_deal.profit != 0:
            await self._account_repo.apply_realised_pnl(
                account_id, net_pnl=matching_deal.profit, at=as_of
            )
        await self._reconciliation_repo.resolve(
            discrepancy_id,
            resolution="closed_from_deal_history",
            resolved_at=as_of,
            resolved_by="reconciler",
        )

    async def _resolve_unknown_at_broker(
        self,
        finding: Finding,
        *,
        run_id: UUID,
        account_id: UUID,
        instrument_id: UUID,
        as_of: datetime,
    ) -> None:
        assert finding.broker_position_id is not None
        discrepancy_id = await self._reconciliation_repo.record_discrepancy(
            reconciliation_run_id=run_id,
            account_id=account_id,
            kind=finding.kind,
            severity=finding.severity,
            local_state=finding.local_state,
            broker_state=finding.broker_state,
            created_at=as_of,
        )

        if finding.matched_intent_id is not None:
            deals = await self._broker.get_deals()
            deal = next(
                d
                for d in deals
                if d.broker_position_id == finding.broker_position_id
                and d.deal_type == DealType.ENTRY
            )
            await self._event_consumer.record_deal(
                deal,
                account_id=account_id,
                instrument_id=instrument_id,
                trade_intent_id=finding.matched_intent_id,
            )
            # record_deal (just above) always creates a position for this
            # exact broker_position_id first, so this is never None in
            # practice - defensive, excluded from branch coverage.
            position_id = await self._position_repo.get_id_by_broker_position_id(
                account_id, finding.broker_position_id
            )
            if position_id is not None:  # pragma: no branch
                details = await self._intent_repo.get_order_details(finding.matched_intent_id)
                await self._position_repo.set_initial_risk(
                    position_id,
                    stop_loss=details.stop_loss,
                    take_profit=details.take_profit,
                    initial_risk=details.risk_amount,
                    updated_at=as_of,
                )
            await self._intent_repo.transition(
                finding.matched_intent_id,
                ExecutionState.FILLED,
                reason="reconciled from broker position",
                actor="reconciler",
                occurred_at=as_of,
            )
            await self._intent_repo.transition(
                finding.matched_intent_id,
                ExecutionState.POSITION_OPEN,
                reason="reconciled from broker position",
                actor="reconciler",
                occurred_at=as_of,
            )
            await self._reconciliation_repo.resolve(
                discrepancy_id,
                resolution="attached_to_intent",
                resolved_at=as_of,
                resolved_by="reconciler",
            )
            return

        # No matching intent at all: someone placed a trade the system never
        # asked for. Record it as ORPHANED and never touch it further.
        assert finding.broker_state is not None
        direction = (
            Direction.LONG
            if finding.broker_state["side"] == OrderSide.BUY.value
            else Direction.SHORT
        )
        row = PositionRow(
            id=uuid4(),
            broker_position_id=finding.broker_position_id,
            account_id=account_id,
            instrument_id=instrument_id,
            trade_intent_id=None,
            signal_id=None,
            direction=direction.value,
            status=PositionStatus.ORPHANED.value,
            volume=Decimal(str(finding.broker_state["volume"])),
            initial_volume=Decimal(str(finding.broker_state["volume"])),
            entry_price=Decimal(str(finding.broker_state["entry_price"])),
            opened_at=as_of,
            updated_at=as_of,
        )
        self._session.add(row)
        await self._session.flush()
        # Left unresolved deliberately: ORPHANED positions raise critical
        # and stay that way until a human acts.

    async def _resolve_sl_mismatch(
        self, finding: Finding, *, run_id: UUID, account_id: UUID, as_of: datetime
    ) -> None:
        discrepancy_id = await self._reconciliation_repo.record_discrepancy(
            reconciliation_run_id=run_id,
            account_id=account_id,
            kind=finding.kind,
            severity=finding.severity,
            local_state=finding.local_state,
            broker_state=finding.broker_state,
            created_at=as_of,
        )
        assert finding.local_state is not None and finding.broker_state is not None
        local_sl = finding.local_state.get("stop_loss")
        broker_sl = finding.broker_state.get("stop_loss")

        if broker_sl is None and local_sl is not None:
            assert finding.broker_position_id is not None
            await self._broker.modify_position(
                finding.broker_position_id, stop_loss=Decimal(str(local_sl))
            )
            await self._reconciliation_repo.resolve(
                discrepancy_id,
                resolution="set_missing_broker_stop",
                resolved_at=as_of,
                resolved_by="reconciler",
            )
            return

        if local_sl is not None and broker_sl is not None:
            # Adopt the broker's value unconditionally on mismatch, per
            # SPEC-06 §6: "someone moved it manually or the broker adjusted
            # it" - the broker is the source of truth once a real order
            # exists there.
            assert finding.local_position_id is not None
            row = await self._session.get(PositionRow, finding.local_position_id)
            if row is not None:
                row.stop_loss = Decimal(str(broker_sl))
                row.updated_at = as_of
                await self._session.flush()
            await self._reconciliation_repo.resolve(
                discrepancy_id,
                resolution="adopted_broker_stop",
                resolved_at=as_of,
                resolved_by="reconciler",
            )
