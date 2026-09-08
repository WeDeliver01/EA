"""SPEC-04 §2-§7: the WebSocket endpoint a real agent connects to.

Handshake (§2): `X-Agent-Key`/`X-Agent-Ts`/`X-Agent-Nonce`/`X-Agent-Signature`
headers on the WS upgrade request, validated against `agents` (api key
hash + decrypted hmac secret) and a Redis-backed nonce store before
`websocket.accept()` - a failure closes the connection without ever
accepting it, so no application-level frame is exchanged with an
unauthenticated caller.

After accept, every inbound frame is an `Envelope` (app.transport.envelope,
mirroring `agent/transport.py`'s). One with a `correlation_id` is a reply
to a command this backend sent (see `app.transport.ws_broker`) and is
routed straight to `AgentConnectionRegistry.resolve` - it never touches
the database here. One without a `correlation_id` is unsolicited
(`event.heartbeat`, `event.deal`) and goes through
`app.transport.event_router.route_unsolicited_event`.

No `hello` frame is sent on accept, unlike SPEC-04 §2's prose: the actual,
live-verified agent (`agent/transport.py`'s `AgentTransport.run()`) treats
every inbound frame uniformly as a command to decode and dispatch via
`on_command`, with no special handling for one - sending it would just
produce a confusing `event.hello_result` echo the backend doesn't need.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import Settings
from app.core.security import verify_agent_signature
from app.execution.event_consumer import EventConsumer
from app.repositories.accounts import AccountRepository
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agents import AgentCredentials, AgentRepository
from app.repositories.deals import DealRepository
from app.repositories.positions import PositionRepository
from app.repositories.trade_intents import TradeIntentRepository
from app.transport.envelope import Envelope
from app.transport.event_router import route_unsolicited_event

router = APIRouter(tags=["agent"])
logger = structlog.get_logger()

_NONCE_TTL_SECONDS = 120


async def _authenticate(websocket: WebSocket) -> AgentCredentials | None:
    headers = websocket.headers
    api_key = headers.get("x-agent-key")
    ts_raw = headers.get("x-agent-ts")
    nonce = headers.get("x-agent-nonce")
    signature = headers.get("x-agent-signature")
    if not api_key or not ts_raw or not nonce or not signature:
        return None
    try:
        ts_millis = int(ts_raw)
    except ValueError:
        return None

    settings: Settings = websocket.app.state.settings
    session_factory = websocket.app.state.session_factory
    async with session_factory() as session:
        creds = await AgentRepository(
            session, encryption_key=settings.agent_secret_encryption_key
        ).get_by_api_key(api_key)
    if creds is None:
        return None

    redis = websocket.app.state.redis
    nonce_is_new = await redis.set(
        f"agent:nonce:{creds.agent_id}:{nonce}", "1", nx=True, ex=_NONCE_TTL_SECONDS
    )
    if not nonce_is_new:
        return None

    if not verify_agent_signature(
        secret=creds.hmac_secret.encode(),
        api_key=api_key,
        ts_millis=ts_millis,
        nonce=nonce,
        signature=signature,
    ):
        return None
    return creds


@router.websocket("/agent/ws")
async def agent_ws(websocket: WebSocket) -> None:
    creds = await _authenticate(websocket)
    if creds is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    registry = websocket.app.state.agent_registry
    registry.register(account_id=creds.account_id, agent_id=creds.agent_id, socket=websocket)
    logger.info(
        "agent_ws.connected", agent_id=str(creds.agent_id), account_id=str(creds.account_id)
    )

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_frame(websocket, raw, creds=creds)
    except WebSocketDisconnect:
        pass
    finally:
        registry.unregister(creds.account_id)
        logger.info(
            "agent_ws.disconnected", agent_id=str(creds.agent_id), account_id=str(creds.account_id)
        )


async def _handle_frame(websocket: WebSocket, raw: str, *, creds: AgentCredentials) -> None:
    try:
        envelope = Envelope.decode(raw)
    except Exception:
        logger.warning("agent_ws.malformed_frame", agent_id=str(creds.agent_id))
        return

    registry = websocket.app.state.agent_registry
    if envelope.correlation_id is not None:
        registry.resolve(
            creds.account_id, correlation_id=envelope.correlation_id, payload=envelope.payload
        )
        return

    session_factory = websocket.app.state.session_factory
    async with session_factory() as session:
        agent_repo = AgentRepository(
            session, encryption_key=websocket.app.state.settings.agent_secret_encryption_key
        )
        event_consumer = EventConsumer(
            agent_event_repo=AgentEventRepository(session),
            deal_repo=DealRepository(session),
            position_repo=PositionRepository(session),
            intent_repo=TradeIntentRepository(session),
        )
        await route_unsolicited_event(
            envelope,
            agent_id=creds.agent_id,
            account_id=creds.account_id,
            agent_repo=agent_repo,
            account_repo=AccountRepository(session),
            event_consumer=event_consumer,
        )
        await session.commit()
