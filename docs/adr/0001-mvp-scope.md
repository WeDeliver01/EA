# ADR 0001: MVP scope for the first build pass

**Status:** Accepted
**Date:** 2026-09-07

## Context

`docs/specs/` is a full technical specification for DelicateTrader, an
autonomous trading platform, laid out as an 11-phase build plan in
`SPEC-10-build-plan.md` (Foundation → Domain → Engine → Research → Paper →
MT5 bridge → Reconciliation/Safety → Terminal → Demo/Live). Phases 3 onward
depend on a live or historical broker feed, a Windows execution host running
MetaTrader5, and weeks-to-months of measurement (SPEC-10 Phase 8 explicitly
says demo and live validation "cannot be shortened"). None of that exists in
this environment, and can't be manufactured in one build session honestly.

This ADR records what "MVP" was scoped to mean here, and every point where
the implementation deliberately deviates from the letter of the spec, so
that a future session (or a human) can pick up from an accurate record
rather than rediscovering these decisions by reading diffs.

## Decision

Build **Phase 0 (Foundation), Phase 1 (Domain and persistence), Phase 2
(Strategy engine), Phase 3 (Research engine) and Phase 4 (Paper execution)**
to their stated acceptance criteria, against a real local PostgreSQL and
Redis, using synthetic (hand-constructed, deterministic) bar data and an
in-process simulated broker rather than a real broker feed or a live MT5
terminal. Stub the repository layout for Phases 5–8 (`agent/`, `ea/`,
`frontend/`, `infra/nginx`, `infra/prometheus`, `docs/runbooks/`) so the
shape described in `SPEC-00` §5 is in place, but do not implement their
logic. This is a foundation to build the trading system on, not a trading
system - nothing in this codebase is connected to a broker, and
`GLOBAL_TRADING_ENABLED` defaults to `false` with no path to `true`
implemented.

**Update:** once a Windows host with a live MT5 terminal and an FTMO demo
account became available, the MT5-facing half of **Phase 5 (the MT5
bridge)** was also built and verified live - see "Phase 5 / MT5 bridge
(partial)" below. The WSS transport to a real backend is still untested,
since no backend is deployed anywhere reachable from that host yet - the
platform is still not connected to a broker end to end.

### Why this boundary specifically

Phase 4 is the last phase whose acceptance criteria are self-contained:
SPEC-10 itself specifies Phase 4 as running "against a simulated broker
with no network" - it does not need a real broker any more than Phase 3's
backtester needed real market data. Phase 5 (MT5 bridge) is a hard stop:
its acceptance criteria explicitly require testing "against a demo
account" on a live MetaTrader5 terminal, which needs a Windows host and a
real broker login that don't exist in this environment and can't be
substituted without lying about what was tested. Phase 8 is explicitly a
measurement phase, not a build phase, needing weeks of real demo/live
trading. Stopping after Phase 4 is the last point where "done" is
verifiable by running tests locally.

## Deviations from the spec, and why

### Phase 0 / infrastructure
- `make up` (Docker Compose) is written to spec but not run end-to-end in
  this environment - no Docker daemon is available here. Verified instead by
  running the same `alembic upgrade head` and `uvicorn app.main:app`
  directly against local Postgres/Redis installs and confirming
  `/system/ready` returns 200 with real DB and Redis checks passing.
- Backend targets Python 3.12 per `SPEC-00` §3 (`pyproject.toml`,
  `Dockerfile`); this environment's default interpreter is 3.11, but 3.12 is
  installed and the venv was built with it.

### Phase 1 / schema
- `CITEXT` extension added (used by `users.email` but not listed in
  `SPEC-02` §0).
- `market_bars` partitions cover 2024-2028, not `SPEC-02`'s literal
  2004..current+1 - there is no historical data to backfill yet, and the
  scheduler service (Phase 6, `SPEC-08` §1) that rolls partitions forward is
  what actually needs to own the far future, not the initial migration.
- `market_ticks` partitions cover 2026-01 through 2027-03 (15 months)
  instead of a full historical range, for the same reason plus the 90-day
  retention `SPEC-02` §10 specifies for live tick capture.
- `rebuild_projections` (`app/repositories/projections.py`) supports one
  round trip per `broker_position_id` (one or more ENTRY deals, then
  EXIT/PARTIAL_EXIT deals that fully close it). Re-entry pyramiding under
  the same broker position id isn't modelled - brokers don't reuse position
  ids across separate trades, so this is a documented simplification, not a
  real-world gap.

### Phase 2 / strategy engine
- No MT5 terminal is available to export indicator CSVs, so `SPEC-05` §5's
  "match an MT5 export to 1e-8 over >= 500 values" requirement is not met.
  `engines/indicators/atr.py` and `adx.py` implement the standard Wilder
  formulas (verified against independently hand-computed values in
  `tests/unit/engines/test_indicators.py`) and would need that MT5
  comparison before being trusted with real money.
- Golden fixtures: `SPEC-05` §1 asks for ~40 MT5-matched `MarketState`
  fixtures. `tests/golden/test_strategy_engine_golden.py` has a much smaller,
  hand-engineered set (an oscillating-market WAIT case and one fully
  engineered TRADE case), because building 40 realistic fixtures needs
  either real market data or a great deal of hand-tuned synthetic data
  engineering. What's there does exercise the full pipeline end to end and
  pins byte-identical serialisation across runs, which is the property the
  spec cares about; it's a smaller sample of it, not a different mechanism.
- `Decision.engine_duration_ms` is always `0` from `evaluate()`. Measuring it
  needs the wall clock, which `evaluate()` may not read (P7, and the purity
  grep forbids `time.time()` under `engines/`). The impure caller (not yet
  built - that's the Phase 4 execution worker) is meant to time the call
  from outside and attach the real duration with `dataclasses.replace()`
  before persisting the `Decision`.
- `TradeConstructor` builds take-profits directly from
  `config.trade_construction.tp_ladder` rather than branching on the
  `tp_mode` enum in `SPEC-05` §3.8. The R-multiple/fraction pairs already
  fully describe a static ladder (`fixed_1r`, `partial_1r_runner`, etc. are
  just different ladders); the two modes that aren't a static ladder at all
  (`atr_trail`, `structure_trail`) are post-fill position management per
  `SPEC-06` §8, not something the engine decides at signal time, so they
  don't belong in `TradeConstructor` regardless.
- `GateEvaluator` is split into `evaluate_pre_construction` and
  `evaluate_post_construction` (`app/engines/gates/strategy_gates.py`). The
  pipeline diagram in `SPEC-05` §2 places `GateEvaluator` (step 8) before
  `TradeConstructor` (step 9), but two of its gates - `RR_BELOW_MINIMUM` and
  `STOP_DISTANCE_INVALID` - need the entry/stop that only `TradeConstructor`
  produces. Both halves land on the same `Decision.gates` tuple, so nothing
  downstream sees the split.
- `EvidenceCollector` has handlers for 7 of the 12 `EvidenceType` values
  (`HTF_TREND_ALIGNMENT`, `LIQUIDITY_SWEEP`, `MANIPULATION_QUALITY`,
  `STRUCTURE_BREAK`, `RETEST_CONFIRMED`, `CANDLE_CONFIRMATION`,
  `MOMENTUM_EXPANSION`) - the ones the default config in `SPEC-05` §4
  actually weights. `SR_ZONE`, `FIB_GOLDEN_ZONE`, `VOLUME_CONFIRMATION`,
  `VOLATILITY_REGIME` and `SESSION_QUALITY` need real broker volume, a
  fibonacci-swing model, and a volatility-regime history this MVP's
  synthetic pipeline doesn't produce; a config that weights one of them
  today just gets no evidence row for it, scoring as absent rather than
  erroring.
- `engines/risk/` implements the parts of `SPEC-06` that are pure functions
  of `RiskState`/`RiskLimits`/`PositionSize` alone: `sizing.py`
  (`calculate_size`, SPEC-06 §2, in full) and `gates.py` for
  `TRADING_DISABLED`, `KILL_SWITCH_ACTIVE`, `DAILY_LOSS_LIMIT`,
  `WEEKLY_LOSS_LIMIT`, `MAX_DAILY_TRADES`, `MAX_OPEN_POSITIONS`,
  `MAX_OPEN_RISK`, `INSUFFICIENT_MARGIN`. The six gates needing live
  inputs this MVP doesn't model - `AGENT_DISCONNECTED`,
  `BROKER_DISCONNECTED`, `RECONCILIATION_UNRESOLVED`, `MARKET_CLOSED`,
  `STRATEGY_PAUSED`, `CORRELATED_EXPOSURE` - are Phase 4-6 work
  (`execution/`, the reconciliation worker) and have no implementation or
  test here. `NEWS_BLACKOUT` and `DUPLICATE_SETUP`, which SPEC-06 §3 also
  lists as risk gates, are evaluated once, as strategy gates
  (`app.engines.gates.strategy_gates`), since `MarketState` already carries
  everything they need - implementing them twice would just create two
  sources of truth.
- Consequently, "every `GateCode` has a triggering test" (`SPEC-10` Phase 2
  acceptance) holds for every gate this MVP implements
  (`tests/unit/engines/test_strategy_gates.py` and `test_risk_gates.py`),
  not for the six deferred risk gates above, which have no code path to
  trigger.
- `engines/risk/` has 100% branch coverage, per SPEC-10 Phase 2's explicit
  requirement; the overall repo coverage gate is 85% (SPEC-08 §6), not 100%.

### Phase 3 / research engine

- **No real market data anywhere.** Every test dataset in
  `tests/unit/research/` is hand-constructed synthetic `Bar` data (flat
  filler bars plus a few deliberately-shaped bars to trigger a fill or a
  take-profit). Nothing here has been run against real XAUUSD history, so
  no conclusion about whether any strategy version has a real edge can be
  drawn from this codebase - that is the whole point of `SPEC-07` §8's v1
  experiment, and it needs a real broker export to mean anything.
- **Fill models**: `next_bar_open` and `pessimistic` (`app/research/fill_model.py`)
  are implemented per spec. `tick` (SPEC-07 §4's "authoritative" model,
  meant to replay real tick data through the spread) has no tick data to
  replay in this environment, so it's a documented stand-in that delegates
  straight to `pessimistic` - it exists so callers can select `"tick"` by
  name without the fill-model registry breaking, not because it does
  anything different yet.
- **Backtester** (`app/research/backtester.py`): single-position-at-a-time
  (a new decision is only evaluated while flat, so `MAX_OPEN_POSITIONS > 1`
  and correlated-exposure gates are untestable here by construction); no
  calendar data (`MarketState.calendar_events` is always empty, so
  `NEWS_BLACKOUT` can never fire in a backtest run); equity is tracked only
  at trade close, not mark-to-market intrabar - floating P&L between bars
  isn't modelled, so the equity curve steps at each realised exit rather
  than moving every bar.
- **Promotion gates** (`app/research/promotion_gates.py`): the function
  itself is complete and unit-tested at every threshold boundary, but four
  of its thirteen inputs are values this MVP pass has no way to compute and
  must be supplied by whatever caller has them: `parameter_plateau_passed`
  (needs Stage 4, not built - see below), `cost_sensitivity_2x_spread_pf`
  and `pessimistic_fill_pf` (need Stage 7 cost-sensitivity re-runs, not
  built), and `data_quality_passed` (the caller's own `check_data_quality`
  call, not re-run internally). The gate function doesn't silently pass
  when these are missing - `None` values fail their gate, per
  `test_promotion_gates.py`.
- **Walk-forward** (`app/research/walk_forward.py`, SPEC-07 §6 Stage 3): no
  parameter optimiser exists in this MVP, so "optimise on train" (itself
  phrased in the spec as "if optimising at all") is a no-op - every fold
  evaluates the same fixed `StrategyConfig` on both its train and test
  windows. Each fold also gets its own independent `BacktestRunner` over
  `[train_start, test_end)`, so a fold's indicator context is limited to
  bars from its own train window onward rather than the dataset's full
  history before it - a real walk-forward would usually keep full lead-in
  context and only restrict the *decision* window. This was chosen over the
  alternative (one continuous run sliced post-hoc into folds) because it
  keeps each fold's balance and risk state genuinely independent, which
  matters more for the efficiency ratio than losing some indicator warm-up.
- **Monte Carlo** (`app/research/monte_carlo.py`, SPEC-07 §6 Stage 5): of
  the five independent randomisation axes the spec lists, two are
  implemented exactly as specified because they operate on the trade list
  the backtester already produced - **trade order** (bootstrap resample
  with replacement) and **trade removal** (drop a random fraction each
  iteration). **Start date** is approximated (each iteration's resample
  pool starts from a trade drawn at random from the dataset's first ~365
  days, not a genuine bar-level restart). **Entry timing shift** (0-3 bars)
  and **slippage resampling** are not implemented at all - both need a full
  backtest re-simulation per iteration (2,000+ re-runs with jittered fill
  timing and a live slippage distribution this MVP has no data for), which
  is a materially larger piece of work than resampling an existing trade
  list.
- **Report generator** (`app/research/report.py`, SPEC-07 §9): assembles
  the outputs of every other research module into one structure, with
  promotion gate results moved to the top per the spec's own instruction.
  It does not render HTML or PDF - that's `SPEC-07` §9's presentation
  concern, which belongs to a later phase (the Terminal, `SPEC-09`), not
  the research engine. Two report sections are not computed anywhere in
  this MVP and are caller-supplied if present: **parameter perturbation
  surfaces** (Stage 4 - needs a sweep harness that re-runs the backtester
  across a parameter grid ±20% in 10 steps per parameter) and the **cost
  sensitivity table** (Stage 7 - needs re-runs at 1.5x/2x spread and 1.5x
  commission). MAE/MFE scatter points are present in the report structure
  but every value is `None`, since `position_simulator.py` doesn't track
  intrabar adverse/favourable excursion - only entry, stop, take-profit
  rungs, breakeven and trailing.
- `Decimal` throughout for every money and percentage figure in `research/`
  (equity, drawdown, net profit, R-multiples), matching P8; `float` only
  where `metrics.py` already documents it (Sharpe/Sortino/Calmar, derived
  statistics, never balances). `monte_carlo.py`'s use of Python's `random`
  module is legitimate here - `research/` is not `engines/`, so the purity
  guard doesn't apply - but every run is seeded (`MonteCarloConfig.seed`)
  for reproducibility.

### Phase 4 / paper execution

- **`SimulatedBroker`** (`app/execution/broker.py`) stands in for the
  Phase 5 agent, implementing the same command/event vocabulary SPEC-04 §4-5
  defines (`place_order`, `modify_position`, `close_position`,
  `get_positions`, `get_deals`, `OrderResult`) so `dispatcher.py`,
  `event_consumer.py` and `position_manager.py` are the exact code that
  would run against a real WSS-connected agent - only the transport
  differs. It is also the "fake agent that can be instructed to misbehave"
  SPEC-06 §10 asks for: every fault (`SILENT`, `REJECT`, `PARTIAL_FILL`) is
  an explicit method a test calls, not a hidden branch.
- **No real transport.** There is no WSS connection, no message envelope
  signing, no reconnection/backoff logic (SPEC-04 §2, §7) - commands and
  events are plain Python calls between in-process objects. Phase 5 is
  where a real transport replaces this; nothing here needs to change
  except which object implements the broker's few methods.
- **No real worker/queue infrastructure.** SPEC-06 §5's `strategy_worker` /
  `execution_worker` / `outbox_dispatcher` / `agent_event_consumer` are
  described as separate async workers reading Redis streams
  (`bars:closed`, `intents:pending`) with `XACK` semantics and Redis locks
  (`LOCK:ANALYSE:*`, `LOCK:EXECUTE:*`). This MVP implements the same
  sequence of steps as plain async functions (`submit_decision`,
  `OutboxDispatcher.dispatch_pending`, `EventConsumer.process_order_result`)
  called directly by tests or, eventually, a real worker loop. The
  concurrency-safety property the locks exist for is still real and still
  tested - `OutboxRepository.claim_pending`'s `FOR UPDATE SKIP LOCKED` is
  what SPEC-06 §10's "two workers consume the same intent" actually
  depends on, and it is proven against real Postgres
  (`test_outbox.py::test_skip_locked_gives_two_concurrent_dispatchers_disjoint_rows`),
  not simulated.
- **Magic number** (SPEC-06 §7) is a `blake2b` hash of
  `(strategy_version_id, instrument_id, environment)`, not the literal
  `(strategy_version_seq << 20) | (instrument_seq << 8) | environment_code`
  encoding - that needs a monotonic sequence-assignment table this MVP
  doesn't build. The property the reconciler actually needs - the same
  triple always producing the same magic, so an orphan can be attributed -
  holds either way.
- **Execution guard** (`OutboxDispatcher.execution_guard`, SPEC-06 §5 step
  19) implements 4 of the 6 fast re-checks: `KILL_SWITCH_ACTIVE`,
  `TRADING_DISABLED`, `DAILY_LOSS_LIMIT`, `RECONCILIATION_UNRESOLVED`. The
  other two - `AGENT_DISCONNECTED` and `BROKER_DISCONNECTED` - need a live
  agent heartbeat that doesn't exist without Phase 5.
- **No day/week-start equity snapshot.** `DAILY_LOSS_LIMIT` and
  `WEEKLY_LOSS_LIMIT` (both in the risk engine and the execution guard)
  compare `realised_pnl_today`/`realised_pnl_week` against *current*
  equity rather than equity snapshotted at the daily/weekly rollover
  (SPEC-06 §3's stated correct behaviour) - no scheduler exists yet to take
  that snapshot (that's Phase 6's reconciliation/scheduler worker).
- **A fill - full or partial - skips `PARTIALLY_FILLED`.**
  `EventConsumer` goes straight `SENT -> ACKNOWLEDGED -> FILLED ->
  POSITION_OPEN` regardless of fill fraction. `SimulatedBroker.place_order`
  resolves an order in exactly one response - there is no real broker
  sending a second, later partial fill for the same order - so "partially
  filled" here means the position opened smaller than requested, not that
  more fills are still coming.
- **Position management is price-driven, not bar- or tick-driven.**
  `position_manager.decide()` takes a current price and ATR the caller
  supplies (the same shape `research/backtester.py` uses per bar), because
  there is no live quote stream. It never decides "the stop was hit" - a
  live broker-side stop order executes itself; that fact would arrive as a
  deal via reconciliation, the same path a manual close does. Not
  implemented: structural invalidation (SPEC-06 §8 step 7, needs the
  `StructureEngine` re-run against live context) and the emergency-spread
  check (needs a live spread reading).
- **Reconciliation** (`app/execution/reconciliation.py`, SPEC-06 §6)
  classifies 7 of the 9 discrepancy kinds: `MISSING_AT_BROKER`,
  `UNKNOWN_AT_BROKER`, `VOLUME_MISMATCH`, `SL_MISMATCH`, `TP_MISMATCH`,
  `PRICE_MISMATCH`, `ORPHAN_INTENT`. `DUPLICATE_POSITION` and
  `BALANCE_MISMATCH` are not classified - both need correlation data (a
  setup-fingerprint history, a broker-reported balance feed) this MVP
  doesn't carry. `ORPHAN_INTENT`'s resolution is a single search pass, not
  SPEC-06 §6's "search, retry, expire after 3 attempts" - the retry count
  needs a place to persist across reconciliation runs that doesn't exist
  yet. The non-negotiable safety property holds exactly as specified: an
  `UNKNOWN_AT_BROKER` position with no matching intent is recorded
  `ORPHANED` and is never, under any code path, closed automatically.
- **Two Phase 1 corrections Phase 4 needed**, both because Phase 4 was the
  first caller to actually exercise these paths:
  - `app/domain/execution/state_machine.py` gained `SENT -> CANCELLED` to
    its allowed transitions. SPEC-06 §5 step 19 requires cancelling an
    already-`SENT` intent when the execution guard blocks it just before
    dispatch; the original Phase 1 transition table only allowed
    `CANCELLED` from `QUEUED`.
  - `DealRepository.list_for_account` (`app/repositories/deals.py`) now
    eager-loads the `trade_intent` relationship it reads in
    `deal_row_to_fill`. Every deal inserted before Phase 4 had
    `trade_intent_id=None`, so the lazy load was never actually attempted;
    the first deal with a real `trade_intent_id` triggered
    `sqlalchemy.exc.MissingGreenlet`.
  - The import-linter layering contract (`pyproject.toml`) moved
    `app.repositories` into its own layer between `app.engines` and
    `app.execution | app.research | app.workers`, which had incorrectly
    modelled repositories as a *sibling* of execution rather than a layer
    it depends on - a contract nobody had violated until Phase 4 needed to
    persist state through it.
- **SPEC-06 §10 chaos scenario coverage**: 11 of the 12 rows are covered by
  named tests (`test_dispatcher.py`, `test_event_consumer.py`,
  `test_reconciliation.py`, `test_outbox.py`, `test_chaos_scenarios.py`).
  Row 11 (clock skew on the agent handshake) is not covered anywhere - it
  is purely Phase 5 territory, since there is no real agent handshake to
  skew the clock of.

### Phase 5 / MT5 bridge (partial)

The MT5-facing half (`agent/`) is built and verified against a live
Windows host running MetaTrader5, logged into an FTMO-Demo account - not
synthetic, not simulated. The backend-facing half (`app/transport/`, the
real WSS gateway) is now built and tested too - handshake, envelope
codec, command/reply correlation, event ingestion, all against real
Postgres/Redis - but the two halves have never been connected to each
other: nothing here has run against a live MT5 terminal, because no
backend has been deployed anywhere reachable from the Windows host yet.

- **`agent/mt5_client.py`** implements `SimulatedBroker`'s exact method
  surface (`place_order`, `modify_position`, `close_position`,
  `get_positions`, `get_deals`) as a thin wrapper over the `MetaTrader5`
  package, plus `get_symbol_spec`, `get_tick`, `terminal_health`, and
  `account_snapshot`. Live-verified against a real broker, which surfaced
  four quirks SPEC-04 doesn't mention: (1) `terminal_info().trade_allowed`
  reflects the terminal's own "Algo Trading" toolbar toggle, not an account
  permission - `_sync_place_order` checks it and rejects locally
  (`LOCAL_NOT_CONNECTED`) rather than sending a doomed order; (2)
  `order_send`'s synchronous result can report `retcode=DONE` with
  `deal=0`/`price=0.0` on this broker's execution mode - the fill is real,
  just not in the synchronous reply, so `_find_deal_by_order` retries
  `history_deals_get(position=...)` up to 10 times (100ms apart) and never
  invents a price; (3) `positions_get(ticket=...)` can briefly return empty
  immediately after another order on the same ticket settles (the
  terminal's local table is mid-update) - `_get_position` retries the same
  way before concluding the position is genuinely gone; (4)
  `history_deals_get`'s date-range arguments are naive datetimes in
  broker-server time, not UTC - `_broker_offset` (computed from a live
  tick, exactly what SPEC-04 §8.4 asks the agent to track for the
  heartbeat) shifts both the query bounds and the returned `executed_at`
  timestamps to true UTC. A related fifth issue: `history_deals_get` raises
  `OSError` on Windows for dates near the 1970 epoch, so the default
  lookback when `since` isn't given is 90 days, not "the beginning of
  time."
- **`agent/store.py`** (SQLite dedup store), **`agent/models.py`** (wire
  dataclasses, not in SPEC-04 §8's file list but added so the agent doesn't
  import `backend.app`), and **`agent/config.py`** (env-driven settings)
  are fully implemented and unit-tested.
- **`agent/transport.py`** has a real, unit-tested HMAC handshake, envelope
  encode/decode, and backoff schedule, but its connect/reconnect loop has
  never connected to an actual server - there is nothing to connect to yet.
  **`agent/executor.py`**, **`agent/watcher.py`**, **`agent/heartbeat.py`**,
  **`agent/health.py`** and **`agent/main.py`** are implemented and wired
  together (verified by import and by inspection, and exercised indirectly
  via `mt5_client`/`store`), but have no dedicated tests of their own yet -
  a gap, not a claim they're fully verified. `main.py` defaults the
  transport to disabled (`--enable-transport`), since pointing it anywhere
  real isn't possible without a deployed backend.
- **31 tests** (`agent/tests/`), all running against `tests/fake_mt5.py` (a
  fake `MetaTrader5` module surface, not a real terminal) so they run
  anywhere, including this repo's own Linux CI - `MetaTrader5` itself is
  a Windows-only dependency (`sys_platform == 'win32'` in
  `agent/pyproject.toml`) and is never imported on any other platform.
  22 cover `mt5_client.py` (filling-mode resolution, every local pre-flight
  rejection, fill mapping including the zero-price fallback lookup,
  modify/close including volume clamping, the broker-time-offset
  conversion for `get_deals` with a frozen clock), 9 cover `store.py`
  (order-result dedup including first-write-wins, the outbound event
  queue, deal-seen tracking, KV round-trip). Not covered by any test:
  `transport.py`'s connect/reconnect loop, `executor.py`'s dispatch logic,
  `watcher.py`'s poll loop, `heartbeat.py`'s payload assembly, `health.py`'s
  HTTP handling, `main.py`'s wiring - none of these have run against a real
  or mocked WebSocket server.
- **Not implemented at all**: reconnection replay (SPEC-04 §7.2-§7.4-
  replaying the local event queue from the last acked `event_id`, then a
  full position snapshot plus a deal batch with 5-minute overlap on
  reconnect; `store.py` has the primitives, nothing calls them yet); rate
  limiting/backpressure on the heartbeat and the `event.quote` throttle
  (SPEC-04 §5's 4/sec max); the agent's dedup replay on
  `command.place_order` is unit-tested at the store layer but has never
  been exercised through the full envelope-decode → executor → store path
  with a real duplicate arriving over the wire.
- **mypy/ruff**: the module originally hard-imported `MetaTrader5` at the
  top level, which made it (and its 22-test file) uncollectable on any
  non-Windows machine - the "31 tests green" the Windows session reported
  had only ever run on that one machine. Fixed by making the import
  optional (`try/except ImportError`) and replacing the three module-level
  `mt5.*` constant lookups with hardcoded MQL5 constants (stable per the
  API, mirrored in `fake_mt5.py`), so the suite now runs, and is gated, on
  this repo's own Linux CI. A related `_run` helper had no return type
  annotation, which was silently erasing every return type through
  `MT5Client`'s async wrapper methods; made generic instead. `agent/`
  now passes `ruff check`, `ruff format --check` and `mypy` clean, same bar
  as `backend/`.

### Phase 5 / the backend's WS gateway (`app/transport/`)

Built against `agent/` exactly as it actually behaves - live-verified and
read line by line - not SPEC-04's prose, which turned out to differ from
the real, tested agent in two ways: reply events are named
`event.<command_name>_result` (e.g. `event.place_order_result`), not
`event.order_result`/`event.position_snapshot`/etc.; and the agent never
set `correlation_id` on a reply, which `app/transport/ws_broker.py` needs
to route a reply to the request that's waiting on it. Fixed on the agent
side (`agent/main.py`'s `on_command`/`emit_event`, a small, safe change -
that code path had no test coverage before or after) rather than worked
around on the backend, since SPEC-04 §3 already specifies `correlation_id`
for exactly this and there's no reason for two independently-maintained
processes to agree on a worse contract than the one already written down.

- **`app/execution/broker.py`** gained `AsyncBroker` (a `Protocol`) and
  `AsyncSimulatedBrokerAdapter`. `dispatcher.py`/`position_manager.py`/
  `reconciliation.py` now depend on and `await` the Protocol instead of
  the concrete `SimulatedBroker` - the seam Phase 4's ADR entry promised
  but that didn't actually exist yet, since every broker call there had
  been a plain synchronous method call. `SimulatedBroker` itself stays
  synchronous (no real I/O; every existing unit test still constructs and
  calls it directly) - the adapter is the only new thing, and every
  existing test's construction site now wraps it.
- **`app/transport/envelope.py`** mirrors `agent/transport.py`'s
  `Envelope` field-for-field. **`app/transport/registry.py`**
  (`AgentConnectionRegistry`) tracks one live connection per account and
  the requests it has in flight, correlated by envelope id -
  `send_command` returns `None` on no connection, agent silence, or a
  mid-flight disconnect, the same `BrokerFault.SILENT` contract
  `SimulatedBroker` already models. **`app/transport/ws_broker.py`**
  (`WSAgentBroker`) implements `AsyncBroker` over the registry -
  `dispatcher.py`/`position_manager.py`/`reconciliation.py` need no
  changes at all to use it instead of `AsyncSimulatedBrokerAdapter`.
  **`app/transport/event_router.py`** routes unsolicited events
  (`correlation_id is None` - not a reply to anything the backend asked
  for): `event.heartbeat` into a new `AgentHeartbeat` row plus
  `Agent.last_seen_at`, `event.deal` into `EventConsumer.record_deal`
  (resolving `instrument_id` from the deal's own `symbol`, via a new
  `AccountRepository.get_instrument_id_by_symbol` - the schema has no
  direct account→instrument reference; SPEC-06 §4's "single instrument"
  is an operational convention, not a stored one). Everything else
  (`event.position_snapshot` unsolicited, `event.quote`,
  `event.terminal_error`, `event.agent_error`, etc.) is logged and
  dropped - a documented gap, not a silent one.
- **`app/api/v1/agent_ws.py`** is the actual `/agent/ws` WebSocket route:
  validates the SPEC-04 §2 handshake (`X-Agent-Key`/`X-Agent-Ts`/
  `X-Agent-Nonce`/`X-Agent-Signature`) against `agents` and a
  Redis-backed nonce store (`SET NX EX 120`, per spec) before ever
  accepting the connection, then routes every frame either to the
  registry (a reply) or the event router (unsolicited). Deliberately
  sends no `hello` frame on accept, unlike SPEC-04 §2's prose: the real
  agent's `AgentTransport.run()` treats every inbound frame uniformly as
  a command to decode and dispatch, with no special handling for one -
  sending it would just produce a confusing, wasted `event.hello_result`
  round trip.
- **`app/repositories/agents.py`** (`AgentRepository`) provisions and
  looks up agent credentials - `create()` returns the plaintext
  `api_key`/`hmac_secret` exactly once (SPEC-04 §2's "shown once"),
  persisting only `api_key_hash` (SHA-256, for exact-match lookup) and
  `hmac_secret_enc` (Fernet, keyed from `AGENT_SECRET_ENCRYPTION_KEY`).
  `hmac_secret` is generated and stored as a `str`
  (`secrets.token_urlsafe(32)`), not raw bytes - the agent reads it from
  `.env` as a string and does `hmac_secret.encode()` before using it as
  the HMAC key (`agent/transport.py`), so generating arbitrary random
  bytes here would have produced a secret that doesn't even round-trip
  through a `.env` file intact, let alone verify. Caught by writing a
  real end-to-end handshake test against the actual route rather than
  only unit-testing the crypto helpers in isolation.
- **Tested for real, not just unit-in-isolation**: `tests/integration/api/
  test_agent_ws.py` runs the actual FastAPI app (real Postgres, real
  Redis) via Starlette's `TestClient` and a real WebSocket handshake -
  accepted with a valid signature, rejected for a bad signature, an
  unknown key, a stale timestamp, a reused nonce, and missing headers -
  plus a live heartbeat round trip landing in the database. Every other
  new module (`envelope`, `registry`, `ws_broker`, `event_router`,
  `AgentRepository`, the handshake crypto in `app/core/security.py`) has
  its own unit or integration tests against realistic, agent-shaped
  payloads, not just internal round-trips.
- **Not implemented**: reconnection replay (SPEC-04 §7.2-§7.4, same gap
  as the agent side - the primitives exist, nothing calls them on
  reconnect); rate limiting/backpressure on inbound `event.quote`; a
  worker loop that actually calls `OutboxDispatcher.dispatch_pending`/
  `Reconciler.run` on a schedule once an agent is connected (nothing
  outside a test has ever called either - a pre-existing gap from Phase
  4, not new here, but still the reason a signal becoming a live order
  isn't fully automatic yet); `get_positions`/`get_deals` requests are
  correlated by envelope id like everything else, which works correctly
  as long as the backend never has two of the same type in flight to the
  same agent at once - true today (nothing issues concurrent ones) but
  worth knowing if that ever changes.

## What is deliberately not built

Phase 3's own Stage 4 (parameter perturbation) and Stage 7 (cost
sensitivity) sub-stages; Phase 4's worker/queue infrastructure (a
scheduler that actually calls `dispatch_pending`/`Reconciler.run`) and the
two discrepancy kinds and retry logic listed above; Phase 5's reconnection
replay and rate limiting/backpressure, on both the agent and backend
sides (listed above); the MQL5 `ea/` fallback; the rest of Phase 6
(kill-switch triggers, alerting, Prometheus metrics, the nightly
determinism replay job); the Next.js `frontend/` terminal (including any
HTML/PDF rendering of the research report); and the demo/live measurement
phases. `infra/` has a working dev `docker-compose.yml` and a stub
`nginx/`/`prometheus/` layout but no production compose file, since
there's still nothing running in `workers/` yet to put behind it.

## Consequences

- The codebase can now run a full paper-trading lifecycle - signal to
  intent to fill to management to close to trade record - against a
  simulated broker and a real Postgres database, with the risk engine
  wired live and every decision it makes reloaded from the database, not
  trusted from a message. It still cannot place a trade with real money:
  there is no connection to a broker anywhere in this repository, and
  `GLOBAL_TRADING_ENABLED` has no effect on anything.
- The research engine can score any strategy version given `Bar` data from
  anywhere, but nothing in `research/` persists a run to Postgres yet - the
  `research_datasets`, `backtest_runs`, `backtest_metrics` and
  `equity_curve_points` tables exist in the schema (Phase 1) with no
  repository layer writing to them from `research/` - and every conclusion
  it could produce today would be about synthetic bars, not XAUUSD. The
  next real increment for the research question is running it against
  actual historical data with a persistence layer, not more in-memory code.
- Both halves of Phase 5 now exist and are independently real: `agent/`
  can place, modify, close and read orders/positions/deals against an
  actual FTMO-Demo account through the actual `MetaTrader5` package, and
  `app/transport/` can authenticate a real WebSocket handshake, dispatch a
  command, correlate its reply, and ingest heartbeats and deals - all
  against real Postgres and Redis. `app/execution/`'s design held up
  exactly as promised: nothing in the dispatcher, event consumer, position
  manager or reconciler needed to change to plug in a real, network-backed
  broker instead of the simulated one. What's still missing is the join:
  a backend deployed somewhere the Windows host can reach, `agent/`
  pointed at it with real credentials from `AgentRepository.create()`,
  and a first real command/reply round trip over an actual socket - until
  that happens, "the two halves talk to each other" is still a design
  claim, not an observed fact. Also still missing: a scheduler that calls
  `dispatch_pending`/`Reconciler.run` on its own (nothing outside a test
  ever has), and reconnection replay on either side.
- Before any of this trades real money, the deferred indicator/MT5
  verification (`SPEC-05` §5), the full golden-fixture set, the full
  `SPEC-07` §8 v1 experiment against real data, and the rest of Phase 5's
  acceptance criteria (a real backend connection, end to end) must all be
  done for real, against an actual broker.
