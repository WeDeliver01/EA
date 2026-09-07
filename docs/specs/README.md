# DelicateTrader: Specification Set

Read `SPEC-00-overview.md` first. It contains the principles and locked decisions that every other document assumes.

| Order | File | Read it when |
|---|---|---|
| 1 | `SPEC-00-overview.md` | Always first. Principles, stack, repo layout, topology |
| 2 | `SPEC-01-domain-model.md` | Building types, enums, state machines |
| 3 | `SPEC-02-database-schema.md` | Writing the initial Alembic migration |
| 4 | `SPEC-03-api-contracts.md` | Building the FastAPI layer or the frontend client |
| 5 | `SPEC-04-agent-protocol.md` | Building the Windows agent or the MQL5 EA |
| 6 | `SPEC-05-strategy-engine.md` | Building the pure engine |
| 7 | `SPEC-06-risk-execution.md` | Building sizing, gates, execution, reconciliation |
| 8 | `SPEC-07-research-engine.md` | Building the backtester and validation |
| 9 | `SPEC-08-infrastructure.md` | Docker, Nginx, env, CI, deployment, ops |
| 10 | `SPEC-09-frontend.md` | Building the terminal |
| 11 | `SPEC-10-build-plan.md` | Deciding what to build next, and when a phase is done |

## The eight principles, in one place

1. Risk outranks conviction.
2. One strategy implementation, shared by backtest, paper and live.
3. Every decision is recorded, including every decision not to trade.
4. Execution is idempotent. The system reconciles rather than assumes.
5. Trading records are immutable.
6. The system defaults to not trading.
7. The engine is deterministic and pure.
8. Money is never a float.

## Build order

```
0 Foundation      -> 1 Domain      -> 2 Engine      -> 3 Research
                                                          |
                                          [ does the edge exist? ]
                                                          |
4 Paper           -> 5 MT5 bridge  -> 6 Safety      -> 7 Terminal
                                                          |
                                                    8 Demo, live micro, scale
```

Phase 3 answers the only question that matters before Phase 4 is worth building. If the 24-configuration matrix in `SPEC-07` §8 produces no version that clears the promotion gates, the correct action is a new hypothesis, not a lower threshold.
