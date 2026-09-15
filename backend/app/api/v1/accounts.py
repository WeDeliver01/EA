"""SPEC-03 §3: account read endpoints. Trading-control endpoints (§4 - the
"dangerous" ones: enable/disable trading, kill switch, close-all) are
deferred - see docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.api.v1.deps import CurrentUser, DbSession
from app.repositories.accounts import AccountRepository

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _summary_json(a: Any) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "label": a.label,
        "broker": a.broker_name,
        "environment": a.environment,
        "currency": a.currency,
        "balance": str(a.balance),
        "equity": str(a.equity),
        "trading_enabled": a.trading_enabled,
        "kill_switch_active": a.kill_switch_active,
    }


@router.get("")
async def list_accounts(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    accounts = await AccountRepository(session).list_accounts()
    return [_summary_json(a) for a in accounts]


@router.get("/{account_id}")
async def get_account(
    account_id: UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    agent_connected = request.app.state.agent_registry.is_connected(account_id)
    detail = await AccountRepository(session).get_account_detail(
        account_id, agent_connected=agent_connected
    )
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")
    body = _summary_json(detail)
    body["mt5_login"] = detail.mt5_login
    body["leverage"] = detail.leverage
    body["agent_connected"] = detail.agent_connected
    return body


@router.get("/{account_id}/state")
async def get_account_state(
    account_id: UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    try:
        state = await AccountRepository(session).load_account_state(
            account_id, as_of=datetime.now(UTC)
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "account_id": str(state.account_id),
        "broker": state.broker,
        "login": state.login,
        "currency": state.currency,
        "balance": str(state.balance),
        "equity": str(state.equity),
        "margin": str(state.margin),
        "free_margin": str(state.free_margin),
        "margin_level": str(state.margin_level) if state.margin_level is not None else None,
        "leverage": state.leverage,
        "server_time": state.server_time.isoformat(),
        "reported_at": state.reported_at.isoformat(),
        "is_stale": state.is_stale,
    }


@router.get("/{account_id}/risk-state")
async def get_risk_state(account_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    try:
        risk = await AccountRepository(session).compute_risk_state(
            account_id, as_of=datetime.now(UTC)
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "as_of": risk.as_of.isoformat(),
        "realised_pnl_today": str(risk.realised_pnl_today),
        "realised_pnl_week": str(risk.realised_pnl_week),
        "open_risk": str(risk.open_risk),
        "trades_today": risk.trades_today,
        "open_position_count": risk.open_position_count,
        "consecutive_losses": risk.consecutive_losses,
        "peak_equity": str(risk.peak_equity),
        "current_drawdown_pct": str(risk.current_drawdown_pct),
        "trading_enabled": risk.trading_enabled,
        "kill_switch_active": risk.kill_switch_active,
    }


@router.get("/{account_id}/exposure")
async def get_exposure(account_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    exposure = await AccountRepository(session).compute_exposure(account_id)
    return {
        "total_open_risk": str(exposure.total_open_risk),
        "by_symbol": [
            {
                "symbol": s.symbol,
                "open_risk": str(s.open_risk),
                "open_position_count": s.open_position_count,
            }
            for s in exposure.by_symbol
        ],
    }
