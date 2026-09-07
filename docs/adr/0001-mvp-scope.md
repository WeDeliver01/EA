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

Build **Phase 0 (Foundation), Phase 1 (Domain and persistence) and Phase 2
(Strategy engine)** to their stated acceptance criteria, against a real
local PostgreSQL and Redis. Stub the repository layout for Phases 3–8
(`agent/`, `ea/`, `frontend/`, `infra/nginx`, `infra/prometheus`,
`docs/runbooks/`) so the shape described in `SPEC-00` §5 is in place, but do
not implement their logic. This is a foundation to build the trading system
on, not a trading system - nothing in this codebase is connected to a
broker, and `GLOBAL_TRADING_ENABLED` defaults to `false` with no path to
`true` implemented.

### Why this boundary specifically

Phase 2 is the last phase whose acceptance criteria are self-contained: pure
Python, a real database, no external service, no data that only a broker or
weeks of demo trading can produce. Phase 3 (research) needs historical tick
data from a real broker; Phase 4 (paper execution) needs the impure
execution/outbox machinery running against a simulated broker over time;
Phase 5 (MT5 bridge) needs a Windows host with MetaTrader5 installed; Phase
8 is explicitly a measurement phase, not a build phase. Stopping after
Phase 2 is the last point where "done" is verifiable by running tests
locally.

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

## What is deliberately not built

Phases 3-8 in full: the backtester and walk-forward/Monte Carlo research
engine, the impure `execution/` order/outbox/reconciliation machinery, the
Windows `agent/` and the MQL5 `ea/` fallback, the Next.js `frontend/`
terminal, and the demo/live measurement phases. `infra/` has a working dev
`docker-compose.yml` and a stub `nginx/`/`prometheus/` layout but no
production compose file, since there's nothing running in `execution/` or
`workers/` yet to put behind it.

## Consequences

- The codebase is safe to run and explore, but cannot place a trade -
  `execution/` is an empty package, and `GLOBAL_TRADING_ENABLED` has no
  effect on anything.
- The next real increment is Phase 3 (research engine) or continuing
  straight to Phase 4 (paper execution against a simulated broker) if the
  strategy question is deliberately deferred - either needs a decision from
  whoever picks this up next, not an assumption baked in here.
- Before any of this trades real money, the deferred indicator/MT5
  verification (`SPEC-05` §5) and the full golden-fixture set must be done
  for real, against an actual broker export.
