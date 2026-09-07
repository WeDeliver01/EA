# SPEC-03: API Contracts

Base path: `/api/v1`. All responses JSON. All timestamps ISO 8601 with `Z`. All decimals serialised as **strings**, never JSON numbers, to avoid float coercion in JavaScript.

---

## 1. Error model

Every non-2xx response uses this envelope:

```json
{
  "error": {
    "code": "RISK_BLOCKED",
    "message": "Daily loss limit reached",
    "detail": { "limit_pct": "0.03", "current_pct": "0.031" },
    "correlation_id": "018f3a2b-7c11-7a3e-9f01-4a2c9de0b1f2"
  }
}
```

| HTTP | Code family | Use |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Malformed input |
| 401 | `UNAUTHENTICATED` | Missing or invalid token |
| 403 | `FORBIDDEN`, `KILL_SWITCH_ACTIVE` | Authenticated but not permitted |
| 404 | `NOT_FOUND` | |
| 409 | `CONFLICT`, `ILLEGAL_STATE_TRANSITION`, `DUPLICATE_INTENT` | |
| 422 | `RISK_BLOCKED`, `GATE_FAILED` | Business rule refusal |
| 429 | `RATE_LIMITED` | |
| 502 | `AGENT_UNAVAILABLE`, `BROKER_ERROR` | |
| 503 | `TRADING_DISABLED`, `RECONCILIATION_UNRESOLVED` | |

Never return 200 with an error body. Never leak broker credentials, HMAC secrets or stack traces.

---

## 2. Auth

```
POST   /auth/login              {email, password, totp?} -> {access_token, refresh_token, expires_in}
POST   /auth/refresh            {refresh_token} -> new pair, old token revoked (rotation)
POST   /auth/logout             revokes the presented refresh token
GET    /auth/me                 -> {id, email, display_name, role}
POST   /auth/totp/enrol         -> {secret, otpauth_uri}
POST   /auth/totp/confirm       {code}
```

Access token: 15 minutes, `HS256` with a key from env, claims `sub`, `role`, `jti`, `exp`, `iat`.
Refresh token: 30 days, rotating, stored hashed, family revoked on reuse detection.
TOTP is **mandatory** for any user with the `operator` role on a `live` account. Enforce at login, not at endpoint level.

Rate limits: 5 login attempts per email per 15 minutes, 60 requests per minute per user for reads, 10 per minute for control actions.

---

## 3. Accounts

```
GET    /accounts                              -> list
GET    /accounts/{id}                         -> AccountDetail
GET    /accounts/{id}/state                   -> live balance, equity, margin, staleness
GET    /accounts/{id}/risk-state              -> RiskState (SPEC-01 §4)
GET    /accounts/{id}/exposure                -> per-symbol and total open risk
PATCH  /accounts/{id}                         {label}   (cosmetic fields only)
```

`AccountDetail` includes `trading_enabled`, `kill_switch_active`, `environment`, active `risk_profile_id`, active strategy assignments, and agent connection status.

---

## 4. Trading control

These are the dangerous endpoints. All require `operator`, all require a fresh TOTP code in the `X-TOTP` header when the account environment is `live`, and all write to `audit_log`.

```
POST   /accounts/{id}/trading/enable          {reason} -> 200
POST   /accounts/{id}/trading/disable         {reason} -> 200
POST   /accounts/{id}/kill-switch/activate    {reason} -> 200
POST   /accounts/{id}/kill-switch/deactivate  {reason, totp} -> 200
POST   /accounts/{id}/positions/close-all     {reason, confirm_phrase} -> 202 {job_id}
POST   /positions/{id}/close                  {reason, volume?} -> 202
POST   /positions/{id}/modify                 {stop_loss?, take_profit?, reason} -> 202
POST   /positions/{id}/breakeven              {reason} -> 202
```

**Semantics that must not be conflated:**

- `kill-switch/activate`: stops **new entries only**. Existing positions continue to be managed normally, including trailing, partials and stop-outs. This is the safe default panic button.
- `positions/close-all`: closes everything at market. Requires `confirm_phrase` to equal the account label exactly. Returns 202 and a job id; the client polls or listens on WebSocket. Partial failures are reported per position, never swallowed.

`kill_switch_active` also blocks the execution worker at the outbox dispatch step, not only at the API. Two independent enforcement points.

---

## 5. Strategy and risk configuration

```
GET    /strategy-versions                          -> list with labels and hashes
POST   /strategy-versions                          {name, semver, config} -> creates, hashes
GET    /strategy-versions/{id}                     -> full config
GET    /strategy-versions/{id}/performance         -> aggregated live + backtest metrics

GET    /accounts/{id}/strategy-assignments
POST   /accounts/{id}/strategy-assignments         {instrument_id, strategy_version_id, primary_timeframe}
POST   /strategy-assignments/{id}/activate         {reason} -> deactivates the previous active one atomically
POST   /strategy-assignments/{id}/pause            {until, reason}
DELETE /strategy-assignments/{id}

GET    /accounts/{id}/risk-profiles                -> version history
POST   /accounts/{id}/risk-profiles                {...limits, reason} -> creates version N+1, inactive
POST   /risk-profiles/{id}/activate                {reason, totp}
```

A strategy version cannot be activated on a `live` account unless it has at least one linked `backtest_run` with `is_out_of_sample = true` whose `backtest_metrics` satisfy the promotion gates in `SPEC-07` §7. Enforce this in the service layer and return `422 GATE_FAILED` with the failing metric names. This is the mechanism that stops an untested idea reaching real money.

---

## 6. Market data

```
GET    /instruments                                     ?broker_id&active
GET    /instruments/{id}
POST   /instruments/{id}/refresh-spec                   pulls contract spec from the agent
GET    /market/bars           ?instrument_id&timeframe&from&to&limit   (max 5000)
GET    /market/quote          ?instrument_id                          -> latest quote + staleness
GET    /market/sessions       ?at                                     -> active sessions
GET    /market/calendar       ?from&to&impact&currency
GET    /market/regime         ?instrument_id&timeframe                -> current regime + inputs
```

Bars are returned oldest-first as arrays of arrays for payload size:
`{"columns":["t","o","h","l","c","v","s"],"rows":[[...], ...]}`.

---

## 7. Analysis, signals, execution

```
GET    /analysis-runs             ?instrument_id&from&to&outcome&limit
GET    /analysis-runs/{id}        -> full decision with evidence, gates, narrative, snapshot
GET    /analysis-runs/{id}/replay -> re-runs the engine on the stored snapshot and diffs
                                     the result against the stored decision

GET    /signals                   ?account_id&from&to&direction&limit
GET    /signals/{id}              -> signal + intent + orders + deals + position + trade

GET    /trade-intents             ?account_id&state&limit
GET    /trade-intents/{id}        -> intent + full transition history
POST   /trade-intents/{id}/cancel {reason}          only valid in QUEUED
POST   /trade-intents/{id}/resolve {resolution, reason, totp}
                                   only valid in UNKNOWN, manual override path

GET    /positions                 ?account_id&status
GET    /positions/{id}            -> position + events + linked deals
GET    /trades                    ?account_id&from&to&strategy_version_id&limit
GET    /trades/{id}
GET    /deals                     ?account_id&from&to
```

`/analysis-runs/{id}/replay` is the determinism test exposed as an endpoint. It must return `{"identical": true}` for every live decision. If it ever returns false, either the engine changed without a version bump or the engine is not pure. Wire it into a nightly job that replays a random sample of 100 live decisions and alerts on any mismatch.

---

## 8. Gate telemetry

```
GET    /telemetry/gate-rejections   ?account_id&instrument_id&from&to
```

Returns counts per `GateCode` with time buckets. This answers "why has the bot not traded in four days" in one request. Surface it prominently in the terminal.

```json
{
  "period": {"from":"2026-09-01T00:00:00Z","to":"2026-09-07T00:00:00Z"},
  "total_evaluations": 672,
  "trades": 3,
  "rejections": [
    {"code":"CONFLUENCE_BELOW_THRESHOLD","count":511,"pct":"0.760"},
    {"code":"REGIME_NOT_PERMITTED","count":98,"pct":"0.146"},
    {"code":"SPREAD_TOO_WIDE","count":41,"pct":"0.061"},
    {"code":"SESSION_NOT_PERMITTED","count":19,"pct":"0.028"}
  ]
}
```

---

## 9. Research

```
POST   /backtests                 {strategy_version_id, instrument_id, dataset_id,
                                   period_start, period_end, kind, cost_model,
                                   fill_model, initial_balance, risk_profile, seed}
                                  -> 202 {backtest_run_id}
GET    /backtests                 ?strategy_version_id&kind&status
GET    /backtests/{id}            -> run config + status + progress
GET    /backtests/{id}/metrics    -> BacktestMetrics
GET    /backtests/{id}/trades     paginated
GET    /backtests/{id}/equity     downsampled equity curve
GET    /backtests/{id}/report     -> full validation report incl. promotion gate results
POST   /backtests/{id}/cancel

POST   /walk-forward              {strategy_version_id, instrument_id, dataset_id,
                                   train_months, test_months, step_months,
                                   optimise: bool, parameter_space?}
                                  -> 202 {parent_run_id}
POST   /monte-carlo               {backtest_run_id, iterations, perturbations[]}
                                  -> 202 {parent_run_id}
POST   /perturbation              {strategy_version_id, parameter, range, steps}
                                  -> 202 {parent_run_id}
GET    /research/compare          ?run_ids=a,b,c   -> side-by-side metric table
```

Research endpoints enqueue Celery tasks. They never block the request. Progress is reported over WebSocket on the `research` channel.

---

## 10. Agent endpoints

Separate router, separate auth scheme, separate rate limits. See `SPEC-04` for the wire format.

```
WS     /agent/ws                          persistent, HMAC handshake, primary transport
POST   /agent/handshake                   HTTP fallback, returns session token
POST   /agent/events                      batch event submission (MQL5 fallback)
GET    /agent/commands                    long-poll, up to 25s (MQL5 fallback)
POST   /agent/ack                         command acknowledgement (MQL5 fallback)
```

Human JWTs are rejected on `/agent/*`. Agent keys are rejected everywhere else. Enforce with two distinct dependency chains, not a shared one with a role check.

---

## 11. System

```
GET    /system/health         -> 200 always if the process is up (for Nginx)
GET    /system/ready          -> 200 only if DB, Redis and migrations are OK
GET    /system/status         -> per-worker last-seen, queue depths, stream lag,
                                 agent connectivity, unresolved discrepancy count
GET    /system/events         ?type&severity&from&to
GET    /metrics               Prometheus text format, bound to loopback only
```

`/system/status` is what the terminal's status bar reads. It must be cheap: everything it reports comes from Redis, not from table scans.

---

## 12. WebSocket

`wss://api.<domain>/api/v1/ws?token=<access_token>`

Client subscribes:

```json
{"type":"subscribe","channels":["account:<uuid>","positions:<uuid>","signals:<uuid>","system","research:<run_id>"]}
```

Server frames:

```json
{"type":"position.updated","channel":"positions:<uuid>","ts":"...","data":{...}}
```

Event types:

| Type | Trigger |
|---|---|
| `account.state` | Every agent heartbeat, throttled to 1/second |
| `quote.tick` | Throttled to 4/second per symbol |
| `analysis.completed` | Every evaluation, includes outcome and score |
| `signal.created` | New signal |
| `intent.state_changed` | Every execution transition |
| `position.opened` / `updated` / `closed` | Position lifecycle |
| `trade.closed` | Round trip complete, includes R multiple |
| `risk.event` | Any `risk_events` row |
| `discrepancy.detected` | Any new discrepancy |
| `agent.connected` / `disconnected` | Agent transport state |
| `system.alert` | Critical `system_events` |
| `research.progress` | Backtest progress percentage |

Server pings every 20 seconds; clients that miss two pongs are dropped. Frames are fanned out from Redis pub/sub so any API replica can serve any client. Reconnecting clients send `{"type":"resume","since":"<ts>"}` and receive missed events from a bounded Redis Stream, capped at 500 frames per channel.
