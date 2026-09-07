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

## What is deliberately not built

Phase 3's own Stage 4 (parameter perturbation) and Stage 7 (cost
sensitivity) sub-stages; Phase 4's real transport, worker/queue
infrastructure, and the two discrepancy kinds and retry logic listed above;
and everything in Phases 5-8: the Windows `agent/` and the MQL5 `ea/`
fallback, the rest of Phase 6 (kill-switch triggers, alerting, Prometheus
metrics, the nightly determinism replay job), the Next.js `frontend/`
terminal (including any HTML/PDF rendering of the research report), and the
demo/live measurement phases. `infra/` has a working dev
`docker-compose.yml` and a stub `nginx/`/`prometheus/` layout but no
production compose file, since there's still nothing running in
`workers/` yet to put behind it.

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
- The next real *build* increment is Phase 5 (the MT5 bridge), which
  cannot be done in this environment at all - it needs a Windows host with
  a MetaTrader5 terminal and a real demo account. Everything this MVP
  built in `app/execution/` is designed so that Phase 5 only has to supply
  a real implementation of the same broker command/event surface
  `SimulatedBroker` already implements; the dispatcher, event consumer,
  position manager and reconciler should not need to change.
- Before any of this trades real money, the deferred indicator/MT5
  verification (`SPEC-05` §5), the full golden-fixture set, the full
  `SPEC-07` §8 v1 experiment against real data, and Phase 5's real-agent
  acceptance criteria must all be done for real, against an actual broker.
