# SPEC-06: Risk Engine, Execution and Reconciliation

This is the part of the system that decides whether real money moves. It has the highest test coverage requirement in the repository: 100 percent branch coverage on `engines/risk/` and `execution/`.

---

## 1. Safety hierarchy, restated as code

```
BROKER              rejects what it rejects, always final
  ^
EXECUTION GUARD     re-validates at dispatch time, immediately before the network call
  ^
RISK ENGINE         approves or blocks, and owns position size
  ^
STRATEGY GATES      hard conditions inside the engine
  ^
CONFLUENCE          weighted score
  ^
PATTERN DETECTION   raw signal
```

Reading the arrows upward: nothing below can override anything above. A `Decision` with `outcome == TRADE` and confluence 10.0 that reaches a risk engine with `DAILY_LOSS_LIMIT` breached produces a `RiskDecision(approved=False)` and a `RISK_BLOCKED` intent. No code path exists to bypass this. Do not write a `force=True` parameter.

---

## 2. Position sizing

The single most bug-prone calculation in retail trading systems. Specify it exactly, then test it exhaustively.

```python
def calculate_size(
    *,
    account_equity: Decimal,
    risk_pct: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    spec: SymbolSpec,
    account_currency: str,
    conversion_rate: Decimal,   # profit currency -> account currency, 1 if same
) -> PositionSize:
```

Steps, in order, all in `Decimal`:

```
 1. risk_amount_target = account_equity * risk_pct
 2. stop_distance      = abs(entry - stop_loss)
 3. if stop_distance <= 0:                    raise InvalidStopDistance
 4. stop_distance_ticks = stop_distance / spec.tick_size
 5. value_per_tick_per_lot = spec.tick_value * conversion_rate
 6. loss_per_lot       = stop_distance_ticks * value_per_tick_per_lot
 7. if loss_per_lot <= 0:                     raise InvalidContractSpec
 8. raw_volume         = risk_amount_target / loss_per_lot
 9. volume = floor_to_step(raw_volume, spec.volume_step)     # ALWAYS floor
10. if volume < spec.volume_min:              return blocked(BELOW_MIN_VOLUME)
11. volume = min(volume, spec.volume_max, risk_limits.max_lot_size)
12. risk_amount_actual = volume * loss_per_lot
13. risk_pct_actual    = risk_amount_actual / account_equity
14. margin_required    = estimate_margin(volume, spec, entry, leverage)
15. if margin_required > account.free_margin * MARGIN_SAFETY_FACTOR:
                                              return blocked(INSUFFICIENT_MARGIN)
```

Non-negotiable details:

- **Step 9 floors, never rounds.** Rounding up on a 0.5 percent risk target silently exceeds the limit. Flooring under-risks, which is the correct direction to be wrong.
- **Step 1 uses equity, not balance.** Balance ignores open floating losses, which is exactly when you most need sizing to shrink.
- **Step 5 uses the broker's `tick_value`.** Never compute pip value from a formula. XAUUSD contract sizes vary by broker and a hard-coded 100 oz assumption is a real way to size 10x wrong.
- **`conversion_rate`** for a ZAR-denominated account trading XAUUSD (profit currency USD) is the live USDZAR rate, fetched from the agent, cached with a 60 second TTL, and stored in `sizing_calculation`. A stale rate is a sizing error.
- **`MARGIN_SAFETY_FACTOR`** default 0.30. Never use more than 30 percent of free margin on one entry.
- **`sizing_calculation`** JSONB stores every intermediate value from steps 1 to 15. When a position turns out to be the wrong size, this is how you find out why in one query.

### Required tests

- Property: `risk_amount_actual <= risk_amount_target` always, for random equity, stops and specs.
- Table test against hand-computed values for XAUUSD, EURUSD, USDJPY (quote currency is the profit currency), GBPJPY (cross), US30, all on a ZAR account and a USD account.
- Boundary: `volume_min`, `volume_max`, `max_lot_size`, `volume_step` of 0.01 and 0.1.
- Regression: a 1-pip stop on a large account must be capped by `max_lot_size`, not produce a 900 lot order.

---

## 3. Hard gates, evaluated in the risk engine

Every gate returns a `GateResult`. All are evaluated; none short-circuit.

| Gate | Condition to pass |
|---|---|
| `TRADING_DISABLED` | `account.trading_enabled` |
| `KILL_SWITCH_ACTIVE` | not `account.kill_switch_active` |
| `STRATEGY_PAUSED` | assignment not paused, or `paused_until` in the past |
| `MARKET_CLOSED` | symbol trading session open per `instruments.trading_hours` |
| `PRICE_STALE` | `now - quote.received_at < QUOTE_STALE_SECONDS` (default 5) |
| `AGENT_DISCONNECTED` | last heartbeat within 10 seconds |
| `BROKER_DISCONNECTED` | last heartbeat reports `terminal_connected` and `trade_allowed` |
| `RECONCILIATION_UNRESOLVED` | zero unresolved `critical` discrepancies for the account |
| `SPREAD_TOO_WIDE` | `spread <= atr * max_spread_atr_multiple` |
| `DAILY_LOSS_LIMIT` | `realised_pnl_today > -(equity_at_day_start * max_daily_loss_pct)` |
| `WEEKLY_LOSS_LIMIT` | same, weekly |
| `MAX_DAILY_TRADES` | `trades_today < max_daily_trades` |
| `MAX_OPEN_POSITIONS` | `open_position_count < max_open_positions` |
| `MAX_OPEN_RISK` | `open_risk + new_risk <= equity * max_open_risk_pct` |
| `CORRELATED_EXPOSURE` | correlated open positions below limit, see §4 |
| `INSUFFICIENT_MARGIN` | step 15 above |
| `RR_BELOW_MINIMUM` | `rr >= min_rr` |
| `STOP_DISTANCE_INVALID` | within `stops_level_points` and the ATR multiple bounds |
| `DUPLICATE_SETUP` | no open position or recent intent with the same fingerprint |
| `NEWS_BLACKOUT` | no high-impact event within the blackout window |

Daily and weekly loss limits are measured against **equity at the period start**, snapshotted at the daily rollover, not against current equity. Measuring against current equity means the limit moves as you lose, which defeats it.

Consecutive-loss pause: when `consecutive_losses >= pause_after_consecutive_losses`, write a `risk_events` row and set `strategy_assignments.paused_until = now + pause_duration_minutes`. This is a circuit breaker, not a gate, because it must persist across evaluations.

---

## 4. Correlated exposure

For v1, single instrument, this is trivially satisfied. Build it correctly anyway because a second symbol will arrive.

Maintain a `correlation_groups` config: `{"USD_LONG": ["EURUSD:short","GBPUSD:short","XAUUSD:long"], ...}`. Compute the net directional exposure per group across open positions and pending intents. Block when adding this trade would exceed `max_correlated_positions` in the same group.

Do not compute rolling correlation from price data at decision time. It is expensive, unstable and produces a gate that behaves differently on identical setups. Use a static, human-reviewed grouping stored in config and revisited quarterly.

---

## 5. Execution flow, end to end

```
strategy_worker
  1. Candle close event on the Redis stream `bars:closed`
  2. Acquire Redis lock  LOCK:ANALYSE:{account}:{symbol}:{tf}  ttl 30s
  3. Build MarketState (market data engine)
  4. decision = engine.evaluate(state)                    PURE
  5. Persist analysis_run + evidence + gates              ONE TRANSACTION
  6. If outcome == WAIT: publish ws event, XACK, done
  7. Persist signal
  8. Publish to stream `intents:pending`
  9. XACK

execution_worker
 10. Consume `intents:pending`
 11. Acquire Redis lock  LOCK:EXECUTE:{account}:{symbol}   ttl 60s
 12. Reload live account state, risk state, open positions FROM DB, not from the message
 13. risk_decision = risk_engine.evaluate(signal, live_state)
 14. If not approved:
         insert trade_intent(state=RISK_BLOCKED) + transition + gates
         publish ws event, XACK, done
 15. client_order_id = ULID()
 16. TRANSACTION:
         insert trade_intent(state=SENT, client_order_id, sizing, snapshots)
         insert execution_transition(DETECTED->VALIDATING->APPROVED->QUEUED->SENT)
         insert outbox(command_type='place_order', idempotency_key=client_order_id)
     COMMIT
 17. XACK   <- acked BEFORE the network call; the outbox now owns the command

outbox_dispatcher (inside execution_worker, separate task)
 18. SELECT ... FROM outbox WHERE dispatched_at IS NULL
         ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 20
 19. EXECUTION GUARD: re-check the fast gates that can change in the seconds
     since step 13:
         kill_switch, trading_enabled, agent connected, spread, price staleness,
         daily loss limit, unresolved critical discrepancy
     If any fail: mark outbox row dispatched with reason, transition intent to
     CANCELLED, done. The trade does not happen.
 20. Send command.place_order over the agent transport
 21. Mark outbox.dispatched_at, increment attempts
 22. Await event.order_result (or the reconciler resolves it later)

agent_event_consumer
 23. Insert into agent_events (unique on agent_id, event_id) - dedup
 24. For order_result:
         insert order row with retcode, raw request and response, latency
         if retcode == DONE:
             insert deal, upsert position, transition intent -> FILLED -> POSITION_OPEN
             enqueue outbox command.modify_position to ensure SL and TP are set
         else:
             transition intent -> REJECTED or BROKER_ERROR, write risk_event
 25. Mark agent_events.processed_at, publish ws events
```

**Why step 16 writes `SENT` before the call:** if the process dies between commit and dispatch, recovery finds a `SENT` intent with an undispatched outbox row and can safely retry, because the `client_order_id` makes the retry idempotent. If the process dies after dispatch but before the result, recovery finds a `SENT` intent with a dispatched outbox row and no order result, transitions it to `UNKNOWN`, and hands it to the reconciler. There is no window where the system does not know what it might have done.

**Why step 12 reloads from the database:** the message on the stream may be seconds old. Between signal creation and execution, a position may have opened, the daily loss limit may have been hit, or a human may have hit the kill switch. Trusting the message contents here is how a system executes a trade it should not have.

---

## 6. Reconciliation

Runs every 30 seconds, and immediately on any of: `open_position_hash` mismatch on a heartbeat, agent reconnection, `UNKNOWN` intent appearing, or a manual trigger.

```
 1. Request command.get_positions and command.get_deals(from=last_deal_ts - 5min)
 2. Build broker_view: positions keyed by broker_position_id, deals by broker_deal_id
 3. Build local_view from the positions table and deals table
 4. Compare and classify:

    In local OPEN, not at broker        -> MISSING_AT_BROKER      critical
    At broker, not in local             -> UNKNOWN_AT_BROKER      critical
    Both, volume differs                -> VOLUME_MISMATCH        critical
    Both, SL differs beyond tolerance   -> SL_MISMATCH            warning
    Both, TP differs beyond tolerance   -> TP_MISMATCH            warning
    Both, entry price differs           -> PRICE_MISMATCH         warning
    Intent SENT/UNKNOWN with no order   -> ORPHAN_INTENT          critical
    Two positions, one fingerprint      -> DUPLICATE_POSITION     critical
    Account balance differs             -> BALANCE_MISMATCH       warning
```

### Automatic resolutions (the only ones permitted)

| Case | Resolution |
|---|---|
| `MISSING_AT_BROKER` and a matching exit deal exists in history | Close the local position from the deal. Resolve. |
| `UNKNOWN_AT_BROKER` and the position's magic and comment match a `SENT` or `UNKNOWN` intent | Attach it to that intent, transition to `POSITION_OPEN`. Resolve. |
| `UNKNOWN_AT_BROKER` with no matching intent | Create the position as `ORPHANED`. Do **not** close it. Raise critical. |
| `SL_MISMATCH` where the broker has no stop and we expect one | Immediately dispatch `command.modify_position` to set it. Resolve, log warning. |
| `SL_MISMATCH` where the broker's stop is tighter than ours | Adopt the broker value. Someone moved it manually or the broker adjusted it. Log. |
| `ORPHAN_INTENT` older than 120 seconds | Search broker history by `client_order_id` in comment, then by magic, symbol, volume and time window. Found: attach. Not found after 3 attempts: transition to `EXPIRED`, resolve. |

Everything else stays unresolved, which activates the `RECONCILIATION_UNRESOLVED` gate and stops new entries until a human acts through `POST /trade-intents/{id}/resolve`.

**The system never automatically closes a position it does not recognise.** An `ORPHANED` position might be a manual trade Ashley placed. Closing it automatically would be the system destroying its operator's intent. Alert loudly, act never.

---

## 7. The magic number

MT5 `magic` is a 64-bit integer available on every order and deal and, unlike the comment, is not modified by brokers. Encode:

```
magic = (strategy_version_seq << 20) | (instrument_seq << 8) | environment_code
```

where `strategy_version_seq` is a monotonic integer assigned per `strategy_versions` row, `instrument_seq` per instrument, and `environment_code` is 1 demo, 2 live. Store the mapping in the database. This lets the reconciler attribute an orphan to a strategy version even when the comment is gone, and lets the account be shared with manual trading without confusion, since manual trades have `magic = 0`.

---

## 8. Position management loop

`position_monitor` runs every 2 seconds per open position, and on every quote update for positions within 1 ATR of a management trigger.

```
for position in open_positions:
    1. If no broker-side SL: dispatch modify immediately. Critical alert.
    2. If spread > emergency_spread_multiple * atr: log, take no action
       (do not close into a spread blowout unless the position is in profit)
    3. If breakeven not moved and unrealised >= breakeven_at_r * initial_risk:
           new_sl = entry +/- breakeven_buffer_atr * atr
           if new_sl is better than current_sl: dispatch modify, set flag
    4. If a TP ladder rung is due and not taken:
           dispatch close_position with volume = fraction * initial_volume
           floor to volume_step; if the result is below volume_min, skip the rung
           and merge it into the next
    5. If trail active (after first TP or after breakeven, per config):
           new_sl = trail calculation
           if new_sl is better than current_sl by >= trail_min_step_atr:
               dispatch modify
    6. If holding_bars >= max_holding_bars: dispatch close, reason TIME_EXIT
    7. If structural invalidation on primary TF: dispatch close, reason STRUCTURAL_EXIT
```

Invariants, tested as properties:

- `new_sl` is applied only if it reduces risk. For a long, `new_sl > current_sl`. For a short, `new_sl < current_sl`. Never the reverse, under any code path.
- Partial closes floor to `volume_step` and never leave a residual below `volume_min`.
- Every modify carries an idempotency key of `f"{broker_position_id}:{field}:{new_value}"` so a redelivered modify is a no-op.
- Step 4 must not fire twice for the same rung. The rung index is persisted on the position, not inferred from remaining volume.

---

## 9. Kill switch and close-all, precisely

**Kill switch active:**
- Execution guard blocks all `place_order` outbox dispatch.
- Risk engine fails `KILL_SWITCH_ACTIVE` on every new evaluation.
- Position monitor continues fully: breakeven, partials, trailing, time exits all still run.
- Rationale: stopping entries is a decision about the future. Abandoning management is a decision to increase risk on existing exposure. They are opposites.

**Close all:**
- Requires the confirmation phrase, TOTP on live, and writes to `audit_log`.
- Dispatches one `close_position` per open position, each with its own idempotency key.
- Reports per-position success or failure. A partial failure is surfaced as a critical alert with the list of positions still open.
- Does **not** activate the kill switch. If the operator wants both, they perform both actions. Bundling them hides one behind the other.

**Automatic kill switch triggers** (system-initiated, all write `risk_events` critical):
- Daily loss limit breached.
- Drawdown from peak equity exceeds `max_drawdown_alert_pct`.
- Agent disconnected for more than 5 minutes.
- Unresolved critical discrepancy for more than 10 minutes.
- Three consecutive `BROKER_ERROR` results within 5 minutes.
- Reconciliation failing to complete for 3 consecutive runs.

---

## 10. Test requirements for this specification

Chaos and failure tests, all against a fake agent that can be instructed to misbehave:

| Scenario | Expected |
|---|---|
| Agent drops the connection immediately after receiving `place_order` | Intent reaches `UNKNOWN`, reconciler resolves it from broker history, exactly one position |
| Agent returns `order_result` twice with the same `event_id` | Exactly one deal, one position |
| Backend crashes between outbox commit and dispatch | On restart, dispatcher sends once, one position |
| Backend crashes after dispatch, before result | Intent to `UNKNOWN`, reconciler resolves |
| Broker rejects with `TRADE_RETCODE_INVALID_STOPS` | `REJECTED`, `risk_event` written, symbol spec refresh queued, no retry |
| Broker fills partially | `PARTIALLY_FILLED`, position sized to actual fill, risk recomputed and logged |
| Human closes the position manually in MT5 | Deal poll detects it, position closed locally, trade recorded, no orphan |
| Human opens an unrelated position in MT5 | `ORPHANED` position, critical discrepancy, entries blocked, nothing auto-closed |
| Broker moves the stop loss | `SL_MISMATCH`, tighter value adopted, warning logged |
| Two workers consume the same intent | Redis lock prevents the second, or the unique constraint on `one_intent_per_signal` does |
| Clock skew of 45 seconds on the agent | Handshake rejected, agent alerts, no trading |
| Daily loss limit hit mid-dispatch | Execution guard cancels at step 19, no order sent |

Each row is a named integration test. None may be skipped in CI.
