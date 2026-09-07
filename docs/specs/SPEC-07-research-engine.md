# SPEC-07: Research Engine

This is built **before** the live execution path. Its purpose is to try to disprove the strategy. A research engine that is optimised to make strategies look good is worse than no research engine, because it converts uncertainty into false confidence.

---

## 1. The equivalence requirement

```python
# The SAME object, in all three modes.
engine = StrategyEngine(config)

# live
decision = engine.evaluate(build_state_from_redis(...))
# paper
decision = engine.evaluate(build_state_from_redis(...))
# backtest
decision = engine.evaluate(build_state_from_history(...))
```

The only difference between modes is the `MarketState` builder and what happens to the `Decision` afterwards. There is no `if backtest:` anywhere in `engines/`.

**Equivalence test, required in CI:** run the backtester over the last 30 days of live data in `replay` mode and diff its `analysis_runs` against the actual live `analysis_runs` for the same strategy version and bar times. Any difference is a bug. This single test catches the entire class of "it worked in backtest" failures.

---

## 2. Backtester architecture

```
BacktestRunner
  ├── DataFeed          replays bars in order, yields only CLOSED bars
  ├── StateBuilder      constructs MarketState identically to live
  ├── StrategyEngine    the same pure engine
  ├── RiskEngine        the same risk engine, against a simulated account
  ├── FillModel         entry and exit price determination
  ├── CostModel         spread, commission, swap, slippage
  ├── PositionSimulator breakeven, partials, trailing, stop and TP resolution
  └── Recorder          writes analysis_runs, trades, equity curve
```

### Event loop

```
for bar in feed:                          # primary timeframe, closed bars only
    1. advance the clock to bar.close_time
    2. update HTF series (only bars whose close_time <= now)
    3. resolve open positions against this bar's intrabar path   <- see §4
    4. build MarketState
    5. decision = engine.evaluate(state)
    6. record analysis_run
    7. if TRADE: risk_engine -> size -> simulated fill at the NEXT bar's open
                 or at the level, per fill model
    8. mark equity curve point
```

**Step 2 is the lookahead trap.** When evaluating an M15 bar closing at 09:15, the H1 bar covering 09:00 to 10:00 has **not** closed. The last closed H1 bar is the one ending at 09:00. Building `MarketState` with the forming H1 bar included is lookahead bias and will make almost any strategy look profitable. Write an explicit test: for a fixed timestamp, assert that every timeframe's last bar has `close_time <= as_of`.

**Step 7: entry is never at the signal bar's close.** The decision is made at the close of bar N. The earliest realistic fill is the open of bar N+1, plus spread and slippage. Any other convention is optimistic.

---

## 3. Data quality gates

A dataset cannot be used for a validated run unless it passes:

| Check | Threshold |
|---|---|
| Missing bars within market hours | < 0.5 percent of expected |
| Largest single gap during market hours | < 4 hours |
| Duplicate timestamps | 0 |
| Bars with `high < low` or `close` outside `[low, high]` | 0 |
| Weekend bars for FX and metals | 0 |
| Zero tick-volume bars | < 2 percent |
| Spread data present | Required for tick fill model |

The quality report is stored on `research_datasets.quality_report` and rendered in the backtest report. A run against a failing dataset is permitted but is permanently flagged `data_quality_failed` and can never satisfy a promotion gate.

**Source the data from the broker you will trade with.** Gold spreads and session behaviour differ enough between brokers to change a marginal strategy's sign. Export via the agent's `command.get_bars` and `command.get_ticks`, which pulls directly from the terminal the system will trade on.

---

## 4. Fill models

Three implementations behind one interface, selected per run and recorded on `backtest_runs.fill_model`.

### `next_bar_open` (fast, for parameter sweeps)
- Entry: next bar's open, plus half the spread, plus fixed slippage.
- Stop and TP: if the bar's range contains both, assume the **stop** hit first. Always.
- Use for coarse exploration only. Never for a promotion decision.

### `tick` (authoritative)
- Replays ticks within each bar. Entry, stop, TP and trailing all resolve against the actual bid and ask sequence.
- Uses the recorded spread at each tick rather than an average.
- Required for any run that supports a promotion decision, and required for any strategy with intrabar management (breakeven, trailing, partials), because bar data cannot tell you whether breakeven was reached before the stop.

### `pessimistic` (stress)
- Entry filled at the worst price in the first N ticks or the bar's adverse extreme.
- Slippage set to the 90th percentile of measured live slippage.
- Stop always assumed hit first on ambiguous bars.
- If the strategy survives this, it has margin. If it only works under `next_bar_open`, it does not have an edge, it has a fill assumption.

**The gap between fill models is itself a metric.** Report `PF(tick) - PF(pessimistic)` on every validation run. A strategy whose profit factor collapses under pessimistic fills is telling you its edge is smaller than its execution uncertainty.

---

## 5. Cost model

```python
@dataclass(frozen=True)
class CostModel:
    spread_source: str           # 'recorded' | 'fixed' | 'distribution'
    fixed_spread_points: int | None
    commission_per_lot_per_side: Decimal
    swap_long_points: Decimal
    swap_short_points: Decimal
    triple_swap_weekday: int     # 2 = Wednesday for most brokers
    slippage_model: str          # 'none' | 'fixed' | 'measured' | 'volatility_scaled'
    slippage_points: int | None
    slippage_distribution: Mapping[str, Any] | None
```

Once live trading begins, `slippage_model='measured'` uses the actual distribution from `trades.entry_slippage_points`. That closes the loop: research is calibrated by live reality rather than by assumption.

Swap must be applied. A strategy holding positions for 22 hours across a Wednesday triple-swap on gold pays real money that a costless backtest hides.

---

## 6. Validation protocol

Every strategy version passes through this, in order. Skipping a stage is not permitted.

### Stage 1: In-sample exploration
Full history, `next_bar_open` fills. Purpose: does the idea produce anything at all. No conclusions drawn.

### Stage 2: Out-of-sample hold-out
The final 30 percent of history is sealed. It is not looked at, not plotted, not used to choose parameters. It is opened once, at the end. If a parameter is changed after opening it, that hold-out is burned and a new one must be carved out of unseen data or a new period must be waited for.

Implement this as an actual guard: `research_datasets` records an `oos_locked_until` timestamp and the runner refuses `is_out_of_sample=True` runs against a dataset whose parameters were modified after the seal.

### Stage 3: Walk-forward
Anchored or rolling, configurable. Default: 24 months train, 6 months test, 6 month step.

For each fold: optimise on train (if optimising at all), evaluate on test, record test-only results. The walk-forward result is the concatenation of all test windows and **nothing else**. Train-window performance is never reported as a result.

Report **walk-forward efficiency** = `expectancy(out-of-sample) / expectancy(in-sample)`. Below 0.5 means the optimisation is fitting noise.

### Stage 4: Parameter perturbation
For each key parameter, sweep plus and minus 20 percent in 10 steps, holding everything else fixed. Produce a surface.

The criterion is not "is the peak high" but "is the peak a plateau". If `atr_stop_multiple = 1.5` gives PF 1.6 and `1.4` and `1.6` give PF 0.9, that is not an edge, that is a coincidence. Formalise it: a parameter passes if PF stays within 25 percent of the peak across at least 60 percent of the swept range.

### Stage 5: Monte Carlo
Minimum 2,000 iterations. Randomise independently:
- **Trade order** (bootstrap resample with replacement): produces the drawdown distribution. Report the 95th percentile max drawdown, not the historical one. The historical drawdown is one sample from this distribution and is almost always optimistic.
- **Entry timing**: shift entry by 0 to 3 bars.
- **Slippage**: sample from the measured or assumed distribution.
- **Trade removal**: randomly drop 10 percent of trades. If the edge disappears, it was concentrated.
- **Start date**: begin at a random point in the first year.

Report: 5th, 50th and 95th percentile of final equity, max drawdown, and profit factor. The number that matters for position sizing is the 95th percentile drawdown, not the historical one.

### Stage 6: Concentration analysis
- Net profit with the single best trade removed.
- Net profit with the best 5 percent of trades removed.
- Net profit with the best year removed.
- Rolling 5-year and 3-year profit factor windows, and how many fell below 1.0.
- Longest period without a new equity high.

This stage exists because a strategy can pass every statistical test and still owe its entire result to one trade or one regime. Report it prominently, not in an appendix.

### Stage 7: Cost sensitivity
Re-run with spread at 1.5x and 2x, commission at 1.5x, and slippage at the 90th percentile. Report the profit factor at each. A strategy whose PF crosses 1.0 at 1.5x spread cannot survive a volatile week.

---

## 7. Promotion gates

A strategy version cannot be activated on a live account unless **all** of the following hold. Implement as a function returning per-gate results, called by `POST /strategy-assignments/{id}/activate`.

| Gate | Threshold |
|---|---|
| Out-of-sample trade count | >= 100 |
| Out-of-sample expectancy | > 0 |
| Out-of-sample profit factor | >= 1.15 |
| Walk-forward efficiency | >= 0.5 |
| Monte Carlo 95th percentile max drawdown | <= 25 percent |
| Profit concentration, single best trade | <= 25 percent of net profit |
| Profit concentration, best 5 percent of trades | <= 60 percent of net profit |
| Profitable periods | >= 60 percent of test windows |
| Parameter plateau | passes for every key parameter |
| Cost sensitivity at 2x spread | profit factor > 1.0 |
| Fill model gap | PF(pessimistic) > 1.0 |
| Data quality | dataset passes §3 |
| Forward demo | >= 30 trading days, >= 30 trades, live-vs-backtest expectancy gap within 30 percent |

The demo gate is checked against actual `trades` rows on a `demo` account, not against a simulation. It cannot be waived.

A version that fails is not blocked from research. It is blocked from money.

---

## 8. The v1 experiment

Three strategy versions, identical in everything except the setup logic, compared under one risk model, one cost model and one dataset:

| Version | Setup |
|---|---|
| A | `BREAKOUT` only. M15 range break, immediate entry. |
| B | `BREAKOUT_RETEST` only. Break, pullback, rejection, entry. |
| C | `BREAKOUT_RETEST` plus regime filter. H4 and H1 alignment plus ATR regime plus session filter. |

Run each through stages 1 to 7. Then run the same three against the exit-mode variants in `SPEC-05` §3.8, which is a 3 by 8 matrix of 24 configurations.

**Report a null result honestly.** If none of the 24 clears the promotion gates, the correct output of this project's research phase is "this family of rules does not have a measurable edge on XAUUSD after costs", and the next step is a different hypothesis, not a lower threshold. Build the report generator so that a failing result is as easy to read and as clearly presented as a passing one.

---

## 9. Report output

Every run produces a report accessible at `GET /backtests/{id}/report` and renderable as HTML and PDF:

1. Run configuration: strategy version hash, engine git SHA, dataset, period, cost model, fill model, seed. Everything needed to reproduce it exactly.
2. Data quality report.
3. Headline metrics table.
4. Equity curve with drawdown underlay, log and linear.
5. Yearly and monthly returns heatmap.
6. R-multiple distribution histogram.
7. MAE and MFE scatter, coloured by outcome. This is what tells you whether stops are too tight.
8. Regime, session and day-of-week breakdowns.
9. Gate rejection counts. How often each gate blocked a trade tells you which filter is doing the work.
10. Concentration analysis.
11. Monte Carlo distribution charts.
12. Parameter perturbation surfaces.
13. Cost sensitivity table.
14. Promotion gate results, pass or fail per gate, at the top of the report, not the bottom.

Point 14 goes first. The reader should see whether it passed before they see a chart that might persuade them it did.

---

## 10. Anti-overfitting rules for whoever runs this

These are process rules, not code, and they are the ones most likely to be broken:

1. Count every configuration you test. With 24 configurations at a 5 percent significance level, roughly one will look significant by chance. Apply a multiple-comparison correction (Bonferroni or Benjamini-Hochberg) or report the deflated Sharpe ratio. The system should track and display the total number of configurations evaluated against a dataset.
2. Never change a parameter after seeing out-of-sample results. That converts the hold-out into training data.
3. Never add a filter to remove a specific losing period. If a filter has no mechanistic justification stated in advance, it is a curve fit with a story attached.
4. Prefer fewer parameters. Each one is a degree of freedom the data must pay for.
5. Write the hypothesis before the test, in `docs/research/`, with a date. Include what result would falsify it. Then run it.
6. Ugly but robust beats beautiful but fragile. A profit factor of 1.12 that holds across 20 years, three brokers and pessimistic fills is a real result. A profit factor of 4.8 over three years is a fitted curve until proven otherwise.
