# DelicateTrader

An autonomous trading platform: Python decides, a risk engine approves,
MT5 executes, PostgreSQL remembers every decision including every decision
not to trade. Full design in [`docs/specs/`](docs/specs/) - start with
[`SPEC-00-overview.md`](docs/specs/SPEC-00-overview.md).

## Status: MVP build, Phases 0-3 of 8

This build covers **Foundation, Domain & persistence, the Strategy engine,
and the Research engine (backtester)** - the parts buildable and testable
without a live broker, an MT5 terminal, or weeks of demo trading. See
[`docs/adr/0001-mvp-scope.md`](docs/adr/0001-mvp-scope.md) for exactly what
that means and every deviation from the spec, and
[`docs/specs/SPEC-10-build-plan.md`](docs/specs/SPEC-10-build-plan.md) for
the full 8-phase plan this is a slice of.

**What works:** the engine can look at a market and produce a fully-reasoned
`TRADE` or `WAIT` decision - regime, structure, liquidity sweeps,
confluence score, every gate it passed or failed, a plain-English narrative
- against a real Postgres schema with an immutable audit trail. That same
engine can now be run through a backtester: fills, costs, position
management (breakeven/partials/trailing/time-exit), metrics, data quality
checks, walk-forward validation, Monte Carlo simulation and promotion gates
all exist and are unit-tested at their boundaries - against synthetic bar
data only, since this environment has no real broker feed (see the ADR).
**What doesn't exist yet:** anything that can act on a decision with real
money, or draw a real conclusion about whether any strategy has an edge.
`execution/` is an empty package, `GLOBAL_TRADING_ENABLED` has no effect on
anything, and there is no connection to a broker anywhere in this
repository.

| Phase | Status |
|---|---|
| 0 Foundation | ✅ Done |
| 1 Domain & persistence | ✅ Done |
| 2 Strategy engine | ✅ Done |
| 3 Research engine | ✅ Done (synthetic data only - see ADR) |
| 4 Paper execution | Not started |
| 5 MT5 bridge | Not started |
| 6 Reconciliation & safety | Not started |
| 7 Terminal | Not started |
| 8 Demo, then live | Not started |

## Repository layout

```
backend/          FastAPI app: domain/, engines/, research/, models/, repositories/, api/
  app/domain/      Pure value objects and enums. Imports nothing else in the project.
  app/engines/     Pure strategy + risk engines. Imports only domain.
  app/research/    Backtester, cost/fill models, metrics, data quality, walk-forward,
                   Monte Carlo, promotion gates, report assembler. Impure (research/ is
                   not engines/) but deterministic given a seed.
  app/models/      SQLAlchemy ORM.
  app/repositories/ DB <-> domain translation. Never leaks ORM instances.
  app/api/         FastAPI routes (currently: /system/health, /ready, /status).
  migrations/      Alembic, one initial schema migration.
  tests/           unit/, integration/ (real Postgres+Redis), golden/ (engine fixtures)
agent/, ea/, frontend/, infra/nginx/, infra/prometheus/, docs/runbooks/
                   Stubbed per docs/specs/SPEC-00 §5. Not implemented - see the ADR.
docs/specs/        The full spec set this was built from.
docs/adr/          Architecture decision records.
```

## Running it

Requires Python 3.12, PostgreSQL 16, Redis 7 (or Docker for all three via
`infra/docker-compose.yml`).

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env   # adjust DATABASE_URL / REDIS_URL if not using Docker
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
curl localhost:8000/api/v1/system/ready
```

Or, with Docker:

```bash
make up      # builds and starts postgres, redis, migrate, api
curl localhost:8000/api/v1/system/ready
make down
```

## Verifying it

```bash
make check   # lint, mypy --strict on domain/engines, import-linter, purity grep, tests
```

- **274 tests**, unit + integration (against real Postgres/Redis) + golden
  engine fixtures.
- **93% overall branch coverage**, **100% on `engines/risk/`** (SPEC-10
  Phase 2's explicit bar).
- `mypy --strict` clean on `domain/` and `engines/`; `mypy` clean on the rest.
- 4/4 import-linter contracts enforcing the dependency rule from
  `SPEC-00` §5 (`domain` imports nothing from the project; `engines` imports
  only `domain`; nothing imports `api`).
- The purity grep (`scripts/check_purity.py`) fails the build on
  `datetime.now()` / `time.time()` / unseeded `random` / `uuid4()` /
  `os.environ` anywhere under `engines/`, and on strategy vocabulary
  (`atr`, `signal`, `confluence`, `strategy`) under `agent/` once that
  package has code in it.

CI (`.github/workflows/ci.yml`) runs all of the above plus the Alembic
upgrade/downgrade/upgrade round-trip on every push.

## The eight principles

Everything here is built to hold these, not just the parts that are
finished:

1. Risk outranks conviction.
2. One strategy implementation, shared by backtest, paper and live.
3. Every decision is recorded, including every decision not to trade.
4. Execution is idempotent. The system reconciles rather than assumes.
5. Trading records are immutable.
6. The system defaults to not trading.
7. The engine is deterministic and pure.
8. Money is never a float.

## Next steps

The research engine (backtester, metrics, walk-forward, Monte Carlo,
promotion gates) is built and unit-tested, but every test in this
repository runs it against synthetic bar data - there's no historical
market data from a real broker in this environment. Before any conclusion
about whether this strategy family has an edge can be trusted, `SPEC-07` §8's
v1 experiment needs to run against real XAUUSD history. Two research-engine
sub-stages also remain unbuilt: parameter perturbation surfaces (SPEC-07 §6
Stage 4) and the cost-sensitivity table (Stage 7) - see the ADR. After that,
per `SPEC-10`, Phase 4 (paper execution against a simulated broker) is the
next build increment.
