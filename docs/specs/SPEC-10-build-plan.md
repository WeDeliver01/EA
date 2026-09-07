# SPEC-10: Build Plan

Each phase has acceptance criteria. A phase is complete when its criteria pass in CI, not when the code looks finished. Do not begin phase N+1 with phase N criteria failing.

---

## Phase 0: Foundation

**Build:** Repo skeleton, `pyproject.toml`, Ruff, mypy, import-linter contracts, Docker Compose with Postgres and Redis, Alembic initialised, Settings model with fail-fast validation, structlog, the `Clock` protocol, CI pipeline, `.env.example`, Makefile.

**Acceptance:**
- `make up` brings the stack up, `/system/ready` returns 200.
- `make check` runs lint, types, import contracts and tests, all passing on an empty test suite.
- The import-linter contract fails the build if `engines/` imports `sqlalchemy`. Prove it with a deliberate violation on a branch.
- Purity grep script fails on a deliberately inserted `datetime.now()` in `engines/`.

---

## Phase 1: Domain and persistence

**Build:** Every domain type in `SPEC-01`. Every table in `SPEC-02` as an Alembic migration. SQLAlchemy models, repositories, the execution state machine with its transition table, triggers, append-only rules.

**Acceptance:**
- `alembic upgrade head` then `downgrade base` then `upgrade head` runs clean.
- Property test: every disallowed execution state transition raises `IllegalStateTransition`.
- Test: `UPDATE` on `deals` is silently a no-op; changing `trade_intents.state` without a transition row raises.
- Test: `rebuild_projections()` reproduces `positions` and `trades` from `deals` exactly, on a seeded fixture of 500 deals.
- Repositories return domain objects, never ORM instances, verified by a type test.

---

## Phase 2: Strategy engine

**Build:** All pipeline stages in `SPEC-05`. Indicators with MT5-verified implementations. `StrategyConfig`. Golden fixtures. Narrative generator.

**Acceptance:**
- 40 golden `MarketState` fixtures produce byte-identical `Decision` serialisations across three consecutive runs and two machines.
- Every indicator matches an MT5 CSV export to 1e-8 over at least 500 values.
- `evaluate()` on a deep copy equals `evaluate()` on the original.
- mypy strict passes on `engines/`.
- Every `GateCode` has at least one test that triggers it.
- A test asserts that for any `as_of`, every timeframe in `MarketState.bars` has `close_time <= as_of`. This is the lookahead guard.
- 100 percent branch coverage on `engines/risk/`.

---

## Phase 3: Research engine

**Build:** Backtester, three fill models, cost model, position simulator, metrics, walk-forward, Monte Carlo, perturbation, concentration analysis, data quality gates, report generator, Celery task wiring, research API endpoints.

**Acceptance:**
- A synthetic dataset with a known, hand-computable outcome produces exactly the expected trades and metrics.
- Lookahead test: a strategy that peeks at the next bar's close scores impossibly well, and a dedicated detector test flags it. Then confirm the real engine cannot access that data structurally.
- Costs test: the same run with zero costs and with realistic costs differs by the exact expected amount.
- Determinism: the same run with the same seed twice produces identical trade lists.
- Walk-forward produces test-window-only results, and a test asserts train-window trades never enter the reported metrics.
- The report generator renders a complete report, including a clearly failing one.
- Promotion gate function returns per-gate pass or fail and is unit tested at each boundary.

**This is the phase where the strategy question actually gets answered.** Run the 24-configuration matrix from `SPEC-07` §8 before starting Phase 4. If nothing clears the promotion gates, stop and change the hypothesis rather than continuing to build execution infrastructure for a strategy that has no edge.

---

## Phase 4: Paper execution

**Build:** Risk engine wired live, intent creation, execution state machine driven end to end, outbox, position manager, performance worker, all against a simulated broker with no network.

**Acceptance:**
- A full lifecycle runs: signal to intent to fill to management to close to trade record.
- All 12 chaos scenarios in `SPEC-06` §10 pass against the fake broker.
- 100 percent branch coverage on `execution/`.
- Sizing test table from `SPEC-06` §2 passes for all six instrument and account-currency combinations.
- Injecting a crash between outbox commit and dispatch, then restarting, produces exactly one order.

---

## Phase 5: MT5 bridge

**Build:** The Python agent in `agent/`, WSS transport with HMAC handshake, single-threaded MT5 access, dedup store, deal polling, heartbeat with position hash, backend agent endpoints and event consumer.

**Acceptance:**
- Against a demo account: place, modify, partially close and close a position, with every deal correctly ingested.
- Kill the agent process mid-order. On restart, the position is correctly attributed with no duplicate.
- Disconnect the network for 90 seconds. On reconnect, the queued events replay and reconciliation reports clean.
- Place a trade manually in the MT5 terminal. It appears as `ORPHANED`, a critical discrepancy is raised, entries are blocked, and nothing is auto-closed.
- Measure and record order latency and slippage distributions over 100 demo orders. Feed them into the research cost model.
- Grep test: the agent package contains no strategy vocabulary.

**Phase 5b, only if required:** the MQL5 fallback EA, to the same acceptance criteria over the HTTP transport.

---

## Phase 6: Reconciliation and safety

**Build:** Reconciliation worker, discrepancy classification and auto-resolution, all hard gates wired, kill switch, close-all, automatic kill triggers, alerting, Prometheus metrics, nightly determinism replay job.

**Acceptance:**
- Every discrepancy kind in `SPEC-02` §7 is produced deliberately in a test and classified correctly.
- Auto-resolutions apply only in the cases listed in `SPEC-06` §6, verified case by case.
- An unresolved critical discrepancy blocks new entries, verified end to end.
- Kill switch blocks entries while the position manager keeps managing, verified with an open position that reaches breakeven while the switch is on.
- Each automatic kill trigger fires under its condition and not otherwise.
- Nightly replay job runs, and a deliberately mutated engine causes it to alert.

---

## Phase 7: Terminal

**Build:** Everything in `SPEC-09`.

**Acceptance:**
- Generated Zod schemas match the live OpenAPI spec; a backend contract change breaks the frontend build.
- WebSocket reconnect shows the disconnected state and dims stale values, verified by cutting the connection.
- Control actions require their confirmations and write `audit_log` rows.
- The decision detail view renders a live decision and its replay diff.
- Lighthouse performance is not a criterion. Correctness of displayed numbers is. Write a test that compares every number rendered on the dashboard against the API response it came from.

---

## Phase 8: Demo, then live

Not a build phase. A measurement phase. It cannot be shortened.

**8a. Demo, minimum 30 trading days.**
- Same executable, same config hash, same everything.
- Daily: record the backtest-versus-demo gap in expectancy, win rate, slippage and fill quality.
- Any code change restarts the 30 day clock. Any config change creates a new strategy version and restarts the clock.
- Exit criterion: the demo gate in `SPEC-07` §7 passes.

**8b. Live micro, minimum 30 trading days.**
- Real money, minimum viable size. `risk_per_trade_pct` at 0.1 percent. The purpose is to measure execution reality, not to make money.
- Measure: demo versus live slippage, rejection rate, latency, requotes, swap accuracy, weekend gap behaviour.
- Exit criterion: live expectancy within 30 percent of demo, zero unexplained discrepancies, zero duplicate orders, zero unmanaged positions.

**8c. Scale.**
- Raise risk in steps: 0.1 to 0.25 to 0.5 percent, each step held for at least 30 trades.
- A step down is triggered automatically by drawdown exceeding the Monte Carlo 50th percentile.
- No step up while any promotion gate has degraded on rolling live data.

**The gate between 8a and 8b is the only irreversible decision in this project.** Everything before it costs time. Past it, it costs money.

---

## Working agreement for Claude Code

1. **One phase per branch.** Do not begin the next phase's work in the current phase's branch.
2. **Tests before implementation** for anything under `engines/risk/` and `execution/`. Those two packages are where a bug costs money.
3. **No `TODO` in `engines/` or `execution/`.** If it is not finished, it is not merged.
4. **No new dependency** without an ADR stating what it replaces and why the stdlib is insufficient.
5. **No `# type: ignore`** in `domain/` or `engines/` without a comment explaining why, reviewed.
6. **Every bug fix ships with the test that would have caught it.** No exceptions in the trading path.
7. **When a spec is ambiguous, write the ADR and pick.** Do not invent a behaviour silently and do not stall. Record the decision.
8. **Never add a code path that bypasses the risk engine.** Not for testing, not for a demo, not behind a flag. Tests inject a configured risk engine; they do not skip it.
9. **Decimal at every boundary.** A float appearing in a sizing calculation is a merge blocker.
10. **If a change alters a golden fixture output, bump the strategy version.** The fixture is the contract.
