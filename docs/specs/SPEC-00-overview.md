# DelicateTrader Platform: Technical Specification

**Version:** 1.0
**Status:** Build specification. This document set is the contract handed to Claude Code.
**Owner:** Ashley
**Repo name:** `delicate-trader`

---

## 0. How to use this document set

| File | Contents |
|---|---|
| `SPEC-00-overview.md` | Principles, locked decisions, repo layout, glossary |
| `SPEC-01-domain-model.md` | Entities, value objects, enums, state machines |
| `SPEC-02-database-schema.md` | Full PostgreSQL DDL, indexes, constraints |
| `SPEC-03-api-contracts.md` | FastAPI endpoints, schemas, error model |
| `SPEC-04-agent-protocol.md` | Backend to MT5 execution agent wire protocol |
| `SPEC-05-strategy-engine.md` | Engine interfaces, pipeline contracts, confluence |
| `SPEC-06-risk-execution.md` | Risk gates, sizing maths, execution state machine, reconciliation |
| `SPEC-07-research-engine.md` | Backtester, walk-forward, Monte Carlo, metrics |
| `SPEC-08-infrastructure.md` | Docker, Nginx, env vars, deployment, monitoring, CI |
| `SPEC-09-frontend.md` | Next.js terminal spec |
| `SPEC-10-build-plan.md` | Phased build with acceptance criteria per phase |

Rules for the implementing agent:

1. Do not deviate from a locked decision in section 3 without writing an ADR in `docs/adr/`.
2. Every phase in `SPEC-10` has acceptance criteria. A phase is not complete until its tests pass.
3. No live broker credentials in any environment until Phase 8.
4. Any file that touches order placement requires a test that proves it is idempotent.

---

## 1. What the system is

An autonomous trading platform where:

- **Python decides.** The strategy engine consumes normalised market state and emits trade candidates.
- **The risk engine approves.** No strategy confidence can override a risk block.
- **MT5 executes.** The execution layer is a thin, dumb arm with no strategy logic in it.
- **PostgreSQL remembers.** Every decision, including every decision *not* to trade, is immutable and replayable.
- **Redis coordinates.** Volatile state, locks, streams, pub/sub to the terminal.
- **Next.js visualises.** Observation and control, never decision-making.

The same strategy engine object is called by the backtester, the paper trader and the live trader. There is exactly one implementation of the strategy. If backtest and live disagree, that is a bug in the data or the execution model, never a second copy of the rules.

---

## 2. Non-negotiable principles

**P1 - Risk outranks conviction.**
Safety hierarchy, highest authority first: Broker → Execution Safety → Risk Engine → Strategy Gates → Confluence → Pattern Detection. A confluence score of 10.0 with the daily loss limit breached is a `NO_TRADE`.

**P2 - One strategy implementation.**
`StrategyEngine.evaluate(MarketState) -> Decision` is pure and side-effect free. It performs no I/O, no clock reads, no randomness. All inputs arrive in `MarketState`. This is what makes backtest and live comparable.

**P3 - Every decision is recorded, including WAIT.**
A rejected setup is a data point. The system records what it saw, which gates passed, which failed, the score, the strategy version, and what price did afterwards.

**P4 - Idempotent execution.**
Every order intent carries a `client_order_id` generated before the network call. A retry with the same id must never produce a second position. The system reconciles rather than assumes.

**P5 - Immutable trading records.**
No `UPDATE` on `signals`, `trade_intents`, `orders`, `deals`. State changes are new rows in a transition log. `positions` is a projection that may be updated but is always rebuildable from `deals`.

**P6 - The system defaults to not trading.**
Unknown state, stale price, disconnected agent, failed reconciliation, unresolved discrepancy: all resolve to `NO_TRADE`. A silent failure must never look like a healthy idle system.

**P7 - Determinism.**
No `datetime.now()` inside domain code. A `Clock` is injected. No unseeded randomness. Same inputs produce byte-identical decisions.

**P8 - Money is never a float.**
`Decimal` in Python, `NUMERIC` in Postgres, at every boundary. Floats are permitted only inside indicator maths on price series, never on account balances, risk amounts, or volumes.

---

## 3. Locked technology decisions

| Layer | Decision | Rationale |
|---|---|---|
| Language | Python 3.12 | MetaTrader5 package compatibility. Not 3.13, not 3.14. |
| API | FastAPI + Uvicorn (behind Gunicorn) | Async, typed, OpenAPI generation |
| ORM | SQLAlchemy 2.0 async + Alembic | Mature, typed, migration discipline |
| Validation | Pydantic v2 | Shared schema between API and agent protocol |
| Database | PostgreSQL 16 | Partitioning for bars, JSONB for evidence payloads |
| Cache and coordination | Redis 7 | Streams, locks, pub/sub, hot state |
| Trading path workers | Dedicated asyncio processes consuming **Redis Streams** consumer groups | Sub-second latency, at-least-once with explicit ack, ordered per stream |
| Research and batch jobs | **Celery** with Redis broker, prefork pool | CPU-bound backtests need process isolation, not the trading path |
| Scheduler | APScheduler inside the `scheduler` service | Candle-close ticks, reconciliation cadence, daily rollovers |
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind | Existing competency |
| Charts | TradingView Lightweight Charts | Candles, levels, trade markers |
| Execution transport | HTTPS + WebSocket, HMAC-signed agent frames | See `SPEC-04` |
| Primary execution agent | **Python agent on Windows VPS using the `MetaTrader5` package** | Full API, async client, no MQL5 limitations |
| Secondary execution agent | **Thin MQL5 EA (`DelicateTrader.mq5`) using `WebRequest` polling** | Required only where the broker or prop firm blocks the Python API or mandates EA-originated orders |
| Auth (human) | JWT access 15 min + rotating refresh 30 d, Argon2id password hashing | Standard |
| Auth (agent) | Long-lived agent key + HMAC-SHA256 request signing with timestamp and nonce | MQL5 can compute HMAC via `CryptEncode`; mTLS cannot be done cleanly in MQL5 |
| Containerisation | Docker Compose | Reproducible, no orchestration overhead at this scale |
| Reverse proxy | Nginx via CloudPanel | Existing infrastructure |
| Errors | Sentry | Backend, workers, frontend |
| Uptime | Uptime Kuma | Agent heartbeat, API health, worker liveness |
| Metrics | Prometheus client endpoint on the API and workers, scraped locally | Latency and gate-rejection counters matter operationally |
| CI/CD | GitHub Actions | Lint, type-check, test, build, deploy on tag |
| Lint and types | Ruff + mypy strict on `domain/` and `engines/` | The trading core must be strictly typed |

### Explicitly deferred

Kubernetes, Kafka, TimescaleDB, microservices, vector databases, multi-broker abstraction, multi-tenancy, cloud managed services. None are needed at this scale. Do not introduce them.

### The Celery-versus-Streams split, stated plainly

The trading path must not run on Celery. Celery's default at-least-once redelivery combined with worker prefetch can replay a task after a broker call has already been made. The trading path uses Redis Streams with explicit `XACK` after the outbox row is marked dispatched, plus the idempotency key in `SPEC-06`. Celery is used only for backtests, walk-forward runs, Monte Carlo, and report generation, none of which touch a broker.

---

## 4. Deployment topology

```
                    Internet
                       |
                   [ Nginx ]  443 only
                       |
        +--------------+---------------+
        |                              |
  trading.<domain>              api.<domain>
   Next.js terminal             FastAPI + WS
                                       |
        +------------------------------+------------------------------+
        |            |            |            |            |         |
   api (x2)     scanner      strategy     execution    position   reconcile
                 worker       worker        worker      monitor     worker
        |            |            |            |            |         |
        +------------+------------+------------+------------+---------+
                       |                              |
                 [ PostgreSQL 16 ]              [ Redis 7 ]
                  bind 127.0.0.1                bind 127.0.0.1

                              ^  WSS, HMAC-signed
                              |
                   +----------+-----------+
                   |  Windows VPS         |
                   |   execution agent    |
                   |   (Python + MT5 pkg) |
                   |          |           |
                   |    MT5 Terminal      |
                   |          |           |
                   |       Broker         |
                   +----------------------+
```

PostgreSQL and Redis bind to loopback only. Nothing but Nginx is exposed. The Windows node initiates the connection outbound to `api.<domain>`; the Linux side never dials the Windows box.

---

## 5. Repository layout

```
delicate-trader/
├── backend/
│   ├── app/
│   │   ├── main.py                 FastAPI app factory
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   ├── errors.py
│   │   │   └── v1/
│   │   │       ├── auth.py  account.py  market.py  signals.py
│   │   │       ├── orders.py  positions.py  trades.py  risk.py
│   │   │       ├── strategy.py  execution.py  agent.py
│   │   │       ├── backtests.py  system.py  ws.py
│   │   ├── core/
│   │   │   ├── config.py           Pydantic Settings, fail-fast validation
│   │   │   ├── security.py         JWT, Argon2id, HMAC agent signing
│   │   │   ├── clock.py            Clock protocol: SystemClock, FrozenClock
│   │   │   ├── logging.py          structlog JSON, correlation ids
│   │   │   ├── redis.py            client, streams, locks
│   │   │   └── exceptions.py
│   │   ├── domain/                 PURE. No I/O, no ORM imports.
│   │   │   ├── market/             Bar, Tick, Symbol, Timeframe, MarketState
│   │   │   ├── strategy/           Decision, Evidence, Confluence, Setup
│   │   │   ├── risk/               RiskDecision, PositionSize, RiskLimits
│   │   │   ├── execution/          OrderIntent, ExecutionState, Fill
│   │   │   └── portfolio/          Position, Exposure, AccountState
│   │   ├── engines/                PURE. Depend only on domain.
│   │   │   ├── market_data/  context/  structure/  liquidity/
│   │   │   ├── manipulation/  evidence/  confluence/  narrative/
│   │   │   ├── risk/  trade_constructor/
│   │   │   └── strategy_engine.py  the single composition root
│   │   ├── execution/              IMPURE. Talks to agent and DB.
│   │   │   ├── order_manager.py  position_manager.py
│   │   │   ├── reconciliation.py  agent_gateway.py  outbox.py
│   │   ├── research/
│   │   │   ├── backtester.py  walk_forward.py  monte_carlo.py
│   │   │   ├── fill_model.py  cost_model.py  metrics.py
│   │   ├── workers/
│   │   │   ├── market_scanner.py  strategy_worker.py
│   │   │   ├── execution_worker.py  position_monitor.py
│   │   │   ├── reconciliation_worker.py  performance_worker.py
│   │   │   └── scheduler.py
│   │   ├── models/                 SQLAlchemy ORM
│   │   ├── repositories/           Data access, returns domain objects
│   │   └── services/               Orchestration, transactions
│   ├── migrations/                 Alembic
│   ├── tests/
│   │   ├── unit/  integration/  contract/  golden/
│   ├── pyproject.toml
│   └── Dockerfile
├── agent/                          Runs on the Windows VPS
│   ├── agent/
│   │   ├── main.py  transport.py  mt5_client.py
│   │   ├── executor.py  reconciler.py  heartbeat.py
│   ├── pyproject.toml
│   └── README.md
├── ea/
│   └── DelicateTrader.mq5          Fallback transport only
├── frontend/
├── infra/
│   ├── docker-compose.yml  docker-compose.prod.yml
│   ├── nginx/  prometheus/
├── docs/
│   ├── adr/  runbooks/
├── .env.example
├── Makefile
└── README.md
```

**Dependency rule, enforced by an import-linter contract in CI:**
`domain` imports nothing from the project. `engines` import only `domain`. `research`, `execution`, `services`, `workers` may import `domain` and `engines`. `api` imports `services`. Nothing imports `api`.

---

## 6. Glossary

| Term | Meaning |
|---|---|
| **Analysis run** | One evaluation of one symbol on one timeframe at one candle close |
| **Decision** | The output of a strategy engine evaluation: `TRADE` or `WAIT`, with full evidence |
| **Signal** | A `TRADE` decision persisted with entry, stop, target and confidence |
| **Trade intent** | A signal that has passed the risk engine and been assigned a `client_order_id` |
| **Order** | An instruction sent to the broker |
| **Deal** | A broker-confirmed execution event (fill). The atomic financial truth |
| **Position** | A projection of open exposure, derived from deals |
| **Trade** | A closed round trip: entry deals through exit deals, with realised P/L |
| **Hard gate** | A binary condition. Any failure means `NO_TRADE` |
| **Soft evidence** | A weighted contributor to the confluence score |
| **Agent** | The process on the Windows VPS that talks to MT5 |
| **Strategy version** | An immutable, hashed configuration of engine parameters |
| **R** | One unit of risk. A 2R win returns twice the amount risked |

---

## 7. Two questions the spec deliberately leaves open

These are strategy parameters, not architectural assumptions. The platform must support either answer without a rewrite.

**Q1 - Trade frequency.** The system is specified for M15 execution with H1 and H4 context, and a tick-level fill model in the backtester. This supports anything from 30 trades a year to intraday frequency. If research selects sub-M5 execution, only the scanner cadence and the agent's tick subscription change. Do not hard-code M15 anywhere outside configuration.

**Q2 - Strategy family.** The engine pipeline is family-agnostic. Breakout, breakout-with-retest and regime-filtered continuation are three `StrategyVersion` configurations of the same code path, compared under an identical risk model. See `SPEC-07` section 8.

---

## 8. Regulatory boundary

The system as specified trades a single account owned by the operator. That is unregulated activity.

The moment the platform issues signals to, or trades on behalf of, any third party, it becomes a regulated financial service in South Africa and requires an FSCA licence. The schema carries `user_id` and `account_id` throughout so that multi-tenancy is a later addition rather than a rewrite, but **no third-party access path may be built** until that question is settled. Do not implement signal sharing, copy trading, public API keys or subscription billing.
