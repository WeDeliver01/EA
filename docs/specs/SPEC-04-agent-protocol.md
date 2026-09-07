# SPEC-04: Execution Agent Protocol

The agent runs on the Windows VPS beside the MT5 terminal. It is the only component permitted to talk to the broker. It contains **no strategy logic, no risk logic and no position management logic**. It executes instructions and reports facts.

---

## 1. Two transports, one protocol

| | Primary: `python_ws` | Fallback: `mql5_http` |
|---|---|---|
| Implementation | Python 3.12 service using the `MetaTrader5` package | `ea/DelicateTrader.mq5` using `WebRequest` |
| Transport | Persistent WSS, bidirectional | HTTP long-poll for commands, HTTP POST for events |
| Command latency | 10 to 60 ms typical | 500 to 2000 ms |
| Tick streaming | Yes, `copy_ticks_from` polling at 100 ms | No, quote snapshots on the poll cycle only |
| Historical data | Full `copy_rates_range` access | Limited, `CopyRates` on the chart symbol |
| Use when | Default | The broker or prop firm blocks the Python API, or requires orders to originate from an EA |

Both speak the same message schema. The backend does not branch on transport except at the connection layer. Build `python_ws` first; the MQL5 EA is Phase 5b and only if needed.

**Why the Python agent is primary:** MQL5 has no native WebSocket client, and `WebRequest` is a synchronous blocking call that stalls the EA thread for the duration of the request. Building the execution path on a blocking HTTP poll inside a single-threaded EA is the wrong foundation for anything time-sensitive. The `MetaTrader5` Python package gives full order, position, history and symbol-spec access with none of those constraints.

**Known constraint:** the `MetaTrader5` package is Windows-only and communicates with a locally running terminal over IPC. It requires "Algo Trading" enabled in the terminal. It cannot run on the Linux VPS. This is why the topology has two machines.

---

## 2. Authentication

Each agent has a record in `agents` with an `api_key` (shown once at creation, stored hashed) and an `hmac_secret` (32 random bytes, stored encrypted at rest with a key from env).

### Handshake

```
Client -> Server (WS connect):
  Header: X-Agent-Key: <api_key>
  Header: X-Agent-Ts: <unix_millis>
  Header: X-Agent-Nonce: <32 hex chars>
  Header: X-Agent-Signature: hex(HMAC_SHA256(secret, f"{api_key}.{ts}.{nonce}"))
```

Server validates:
1. `api_key` hash matches an active agent.
2. `abs(now - ts) <= 30_000` ms.
3. `nonce` unseen: `SET agent:nonce:{agent_id}:{nonce} 1 NX EX 120`. Reuse is rejected.
4. Signature matches.

On success the server sends:

```json
{"type":"hello","agent_id":"...","account_id":"...","server_time":"...",
 "protocol_version":1,"heartbeat_interval_ms":2000,
 "resume_from_event_id":"<last processed agent event id>"}
```

Every subsequent frame from the agent carries `seq` (monotonic per connection) and `event_id` (ULID, stable across reconnects for the same logical event). Frames are also individually signed for the HTTP fallback; over WSS the handshake signature covers the session.

### Rotation

`POST /agent/rotate-key` from an authenticated operator issues a new key and secret with a 24 hour overlap window. The agent picks up the new pair on next restart. Never ship keys in the repo or the EA source; the EA reads them from a terminal input parameter, the Python agent from `.env`.

---

## 3. Message envelope

```json
{
  "v": 1,
  "type": "command.place_order",
  "id": "01JCXG4Q7T8N2M9RZKWDVYE3PA",
  "correlation_id": "018f3a2b-...",
  "ts": "2026-09-07T09:14:22.318Z",
  "payload": { }
}
```

`id` is the idempotency key for commands (equals `client_order_id` for order placement) and the deduplication key for events.

---

## 4. Server to agent commands

### `command.place_order`

```json
{
  "client_order_id": "01JCXG4Q7T8N2M9RZKWDVYE3PA",
  "symbol": "XAUUSD.r",
  "side": "BUY",
  "order_type": "MARKET",
  "volume": "0.12",
  "limit_price": null,
  "stop_loss": "3412.550",
  "take_profit": "3428.100",
  "max_slippage_points": 30,
  "magic": 730914221,
  "comment": "01JCXG4Q7T8N2M9RZ",
  "expires_at": null
}
```

Agent behaviour, in strict order:

1. **Dedup check.** If `client_order_id` is in the local SQLite dedup store, do not send. Reply with the stored result. The store survives agent restarts.
2. **Record intent locally** to SQLite with status `sending`, before the broker call.
3. Verify `symbol` is selected in Market Watch (`symbol_select`), terminal connected, `trade_allowed` true.
4. Verify `volume` conforms to `volume_min`, `volume_max`, `volume_step`. If not, reject locally with `INVALID_VOLUME`. Do not silently round; the backend is responsible for rounding and a mismatch means a stale spec.
5. Verify `stop_loss` distance respects `stops_level_points`. If violated, reject with `STOPS_LEVEL_VIOLATION` and report the broker's current value so the backend can refresh the spec.
6. Send `order_send` with `deviation = max_slippage_points`, `type_filling` resolved from `symbol_info.filling_mode` (try `FOK`, then `IOC`, then `RETURN`).
7. Record the `retcode`, `order` ticket, `deal` ticket and full raw result to SQLite, then emit `event.order_result`.

**If step 6 raises or times out:** mark the local record `unknown`, emit `event.order_unknown`, and immediately begin local recovery: poll `history_deals_get` and `positions_get` for 60 seconds looking for a deal whose `comment` starts with the first 17 characters of `client_order_id` or whose `magic` matches and whose symbol, side, volume and time window fit. Emit `event.order_result` when found, `event.order_not_found` if not.

**The agent never retries `order_send` on its own.** Retry is a backend decision made after reconciliation.

### Other commands

```
command.modify_position     {broker_position_id, stop_loss?, take_profit?}
command.close_position      {broker_position_id, volume?, max_slippage_points}
command.close_all           {reason}
command.cancel_order        {broker_order_id}
command.get_positions       {}
command.get_orders          {}
command.get_deals           {from, to}
command.get_symbol_spec     {symbol}
command.get_bars            {symbol, timeframe, from, to, count}
command.get_ticks           {symbol, from, to}
command.subscribe_symbols   {symbols[], stream_ticks: bool}
command.ping                {}
```

All commands carry an `id`. All are idempotent by that id. `command.close_position` deduplication matters as much as placement: a duplicated close on a hedging account opens an opposite position.

---

## 5. Agent to server events

### `event.heartbeat` (every 2 seconds)

```json
{
  "agent_time": "2026-09-07T09:14:22.318Z",
  "broker_time": "2026-09-07T12:14:22Z",
  "terminal_connected": true,
  "trade_allowed": true,
  "algo_trading_enabled": true,
  "build": 4620,
  "account": {
    "login": "51234567", "currency": "USD", "leverage": 500,
    "balance": "10000.00", "equity": "10142.30",
    "margin": "212.40", "free_margin": "9929.90", "margin_level": "4775.10"
  },
  "open_position_count": 1,
  "open_position_hash": "sha256 of sorted (position_id, volume, sl, tp)",
  "symbols": [{"symbol":"XAUUSD.r","bid":"3418.22","ask":"3418.51","time":"..."}],
  "agent_version": "1.4.2"
}
```

`open_position_hash` is the cheap reconciliation primitive. The backend computes the same hash from its own state on every heartbeat. A mismatch triggers a full reconciliation immediately rather than waiting for the scheduled run. Two seconds of drift, not sixty.

### Other events

```
event.order_result       {client_order_id, retcode, retcode_text, broker_order_id,
                          broker_deal_id, broker_position_id, filled_volume,
                          fill_price, requested_price, slippage_points, latency_ms, raw}
event.order_unknown      {client_order_id, error, elapsed_ms}
event.order_not_found    {client_order_id, searched_from, searched_to}
event.order_rejected     {client_order_id, reason, broker_retcode}
event.deal               {full Fill payload}         emitted for every new deal seen
event.position_snapshot  {positions[]}               response to get_positions
event.position_changed   {broker_position_id, field, old, new, source}
                          source: 'us' | 'broker' | 'manual'
event.symbol_spec        {full SymbolSpec}
event.bars               {symbol, timeframe, columns, rows}
event.ticks              {symbol, columns, rows}
event.quote              {symbol, bid, ask, time}     throttled, 4/sec max
event.terminal_error     {code, message, context}
event.agent_error        {code, message, traceback_hash}
```

### Deal polling

Independently of any command, the agent polls `history_deals_get` every 2 seconds for a rolling 10 minute window and emits `event.deal` for anything it has not sent before, tracked in local SQLite. This is how manual trades placed by a human in the terminal, broker-side stop-outs and margin closures reach the backend. Without it, the system's picture of reality is only as good as its own actions, which is exactly the failure P4 exists to prevent.

---

## 6. Delivery semantics

| Direction | Guarantee | Mechanism |
|---|---|---|
| Server to agent | At least once | Outbox row, dispatched until acked, unique on `(command_type, idempotency_key)` |
| Agent to server | At least once | Local SQLite queue, retried until server acks by `event_id` |
| Effect on broker | Exactly once | Agent-side dedup store keyed on `client_order_id` |

The agent acks a command only after the broker result is durably written to its local SQLite. The backend acks an event only after the `agent_events` row is committed. Neither side acks on receipt.

Local SQLite on the Windows box (`agent_state.db`) holds: dedup records, the outbound event queue, the last processed command id, and the last seen deal ticket. It is the agent's crash-recovery memory and must be on the same disk as the terminal, backed up daily.

---

## 7. Reconnection

1. Exponential backoff with jitter: 1s, 2s, 4s, 8s, 16s, 30s, capped at 30s.
2. On reconnect, replay the local event queue from the last server-acked `event_id`.
3. Immediately emit a full `event.position_snapshot` and an `event.deal` batch covering the disconnected window plus a 5 minute overlap.
4. The backend marks the agent connected only after the snapshot is processed and reconciled, not on socket open.

**Backend behaviour while an agent is disconnected**, by elapsed time:

| Elapsed | Action |
|---|---|
| 0 to 10s | Normal. Transient. |
| 10 to 30s | `AGENT_DISCONNECTED` hard gate active. No new entries. Warning event. |
| 30s to 5 min | Critical alert. Position monitor continues to compute but cannot act. Broker-side SL and TP are the only protection, which is why they must always be set. |
| Over 5 min | `risk_events` critical row, notification, and `trading_enabled` is set to false requiring manual re-enable |

The system does not panic-close on disconnect. It cannot: it has no route to the broker. This is precisely why every position carries a real broker-side stop from the moment of fill.

---

## 8. Agent internal design (`agent/`)

```
agent/
├── main.py           asyncio entrypoint, supervises tasks, graceful shutdown
├── config.py         env-driven settings
├── transport.py      WSS client, HMAC handshake, backoff, frame codec
├── mt5_client.py     Thin wrapper over the MetaTrader5 package.
│                     ALL calls run in a single dedicated thread via a
│                     run_in_executor with a lock. The MT5 package is NOT
│                     thread-safe and NOT async.
├── executor.py       Command handlers, dedup store, order placement
├── watcher.py        Deal polling, position snapshot, quote streaming
├── heartbeat.py      2s heartbeat with position hash
├── store.py          SQLite dedup + outbound queue
└── health.py         Local HTTP /health on 127.0.0.1:8799 for Uptime Kuma
```

Non-negotiable implementation notes:

1. **Single-threaded MT5 access.** All `mt5.*` calls go through one worker thread behind an `asyncio.Lock`. Concurrent calls into the package produce undefined behaviour.
2. **`mt5.initialize()` health checking.** Poll `mt5.terminal_info()` and `mt5.account_info()` every heartbeat. If either returns `None`, reinitialise. If reinitialise fails 3 times, emit critical and exit non-zero so the Windows service manager restarts the process.
3. **Run as a Windows Service** via NSSM or `sc.exe`, with automatic restart on failure and restart-on-boot. The MT5 terminal itself must also auto-start and auto-login. Document both in `docs/runbooks/windows-node.md`.
4. **Broker time offset.** Compute and report `broker_time - agent_time` every heartbeat. The backend stores it on `brokers.timezone_offset_minutes`. All broker timestamps are converted to UTC on ingestion, never stored raw. DST changes on broker servers are a real and recurring source of off-by-one-hour bugs.
5. **No decisions.** The agent must not contain a threshold, a filter, or a condition that changes whether a trade happens. Grep the agent package for the words "atr", "signal", "confluence", "strategy" in CI and fail the build if found.

---

## 9. MQL5 fallback EA (`ea/DelicateTrader.mq5`)

Build this only if a broker or prop firm requires it. Scope, deliberately minimal:

```
OnInit()      read inputs (api_url, api_key, hmac_secret, magic), validate WebRequest
              whitelist, EventSetTimer(1)
OnTimer()     1. POST /agent/events with queued events (account, positions, deals, quote)
              2. GET  /agent/commands (long-poll, 5s server-side wait)
              3. Execute any returned command via CTrade
              4. POST /agent/ack
OnTradeTransaction()  queue deal and order events immediately
OnDeinit()    flush queue, notify server
```

Constraints to design around:
- `WebRequest` is blocking. Keep the timer at 1 second and the server-side long-poll at 5 seconds maximum, or the EA stalls.
- The target URL must be added to the terminal's allowed-URL list manually. Document it.
- `CryptEncode(CRYPT_HASH_SHA256, ...)` provides the HMAC building block; implement HMAC-SHA256 as the standard ipad/opad construction over it.
- Order comments are truncated by many brokers and some overwrite them entirely. Never rely on the comment alone for correlation. Use `magic` plus a local `client_order_id`-to-ticket map persisted with `FileWrite` to the common folder.
- Queue events to a local file so a terminal restart does not lose them.

Maximum acceptable size: roughly 800 lines. If the EA is growing past that, strategy logic has leaked into it and the design has failed.

---

## 10. Security posture

- The Windows node initiates all connections outbound. No inbound ports are open on it. RDP is restricted to a specific source IP or, preferably, reached through Tailscale or a WireGuard tunnel.
- Agent keys are scoped to a single `account_id`. A compromised agent key can act on one account, and only through commands the backend originates.
- Nothing in the agent can enable trading, change a risk limit, or place an order the backend did not command. The agent has no autonomous path to the market.
- Log the full raw broker request and response on every order to `orders.raw_request` and `orders.raw_response`. When a broker disputes a fill, that record is the argument.
