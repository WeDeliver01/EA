# SPEC-05: Strategy Engine

The strategy engine is a pure function. Given the same `MarketState` and the same `StrategyConfig`, it returns a byte-identical `Decision`. Everything in this document exists to protect that property.

---

## 1. The contract

```python
# engines/strategy_engine.py
class StrategyEngine:
    def __init__(self, config: StrategyConfig) -> None: ...

    def evaluate(self, state: MarketState) -> Decision:
        """Pure. No I/O, no clock, no randomness, no logging side effects,
        no mutation of `state`. Deterministic in config and state alone."""
```

Enforced by:

- `import-linter` contract: `engines` may import `domain` and stdlib only. Not `sqlalchemy`, not `redis`, not `httpx`, not `datetime.now`.
- A CI grep failing the build on `datetime.now`, `time.time`, `random.` (without a seed), `uuid4` and `os.environ` anywhere under `engines/`.
- A golden test suite: ~40 stored `MarketState` fixtures with their expected `Decision` serialisations. Any change to the engine that alters a golden output must either be accompanied by a strategy version bump or be rejected in review.
- A property test: `evaluate(state) == evaluate(state)` and `evaluate(deepcopy(state)) == evaluate(state)`.

---

## 2. Pipeline

Each stage is a class with a single method. Stages compose in a fixed order. Every stage emits both a result and its inputs, so that the narrative and the audit trail can be generated without re-running anything.

```
MarketState
    |
 1  ContextEngine        -> Context      (regime, HTF bias, ATR percentiles, session)
    |
 2  StructureEngine      -> Structure    (swings, ranges, key levels, break state)
    |
 3  LiquidityEngine      -> Liquidity    (equal highs/lows, session H/L, PDH/PDL,
    |                                     sweep candidates)
 4  ManipulationEngine   -> Manipulation (sweep-and-reject detection, quality score)
    |
 5  SetupDetector        -> Setup | None (breakout / retest / pullback continuation)
    |
 6  EvidenceCollector    -> tuple[Evidence, ...]
    |
 7  ConfluenceEngine     -> score, band
    |
 8  GateEvaluator        -> tuple[GateResult, ...]   (strategy gates only)
    |
 9  TradeConstructor     -> entry, stop, TP ladder, RR
    |
10  NarrativeGenerator   -> human-readable explanation
    |
Decision
```

The risk engine is **not** in this pipeline. It runs after, in the impure layer, because it needs live account state and must be able to block a decision the strategy already made. That separation is P1 made structural.

---

## 3. Stage specifications

### 3.1 ContextEngine

Outputs:

```python
@dataclass(frozen=True, slots=True)
class Context:
    regime: Regime
    htf_bias: Direction | None          # from H4
    itf_bias: Direction | None          # from H1
    atr: Mapping[Timeframe, Decimal]
    atr_percentile: Decimal             # current ATR vs its own trailing N-period rank
    atr_expanding: bool
    adx: Decimal
    range_pct_of_atr: Decimal
    session: Session
    bars_since_session_open: int
    inputs: Mapping[str, Any]           # every intermediate value, for the audit trail
```

Regime classification, as a decision table rather than nested conditionals:

| ATR percentile | ADX | HTF vs ITF | Regime |
|---|---|---|---|
| < 25 | < 20 | any | `COMPRESSION` |
| any | >= 25 | aligned | `TRENDING_UP` / `TRENDING_DOWN` |
| > 75 | any | any | `EXPANSION` |
| 25 to 75 | < 20 | any | `RANGING` |
| any | any | conflicting | `CHOPPY` |
| insufficient bars | - | - | `UNKNOWN` |

Thresholds come from config, never literals in code. `UNKNOWN` and `CHOPPY` are `NO_TRADE` by default, overridable per strategy version.

### 3.2 StructureEngine

Swing detection uses a fractal with a configurable lookback (`swing_lookback`, default 3 bars either side). Confirmed swings only: a swing high is confirmed `swing_lookback` bars after it forms. Using unconfirmed swings is lookahead bias and the most common way a backtest lies.

```python
@dataclass(frozen=True, slots=True)
class Structure:
    swing_highs: tuple[SwingPoint, ...]
    swing_lows: tuple[SwingPoint, ...]
    trend_state: str            # HH_HL, LH_LL, MIXED
    range_high: Decimal | None
    range_low: Decimal | None
    range_bars: int
    last_break: StructureBreak | None
    key_levels: tuple[Level, ...]    # PDH, PDL, PWH, PWL, session H/L, round numbers
```

A `StructureBreak` requires a **close** beyond the level, not a wick, unless `break_on_wick` is enabled in config. Store both interpretations in `inputs` so the research engine can compare them without a code change.

### 3.3 LiquidityEngine

Identifies where stops are likely resting: equal highs and lows within a tolerance band, prior session extremes, previous day and week extremes, and obvious round numbers. Output is a set of `LiquidityPool` objects with a price, a type and an estimated significance derived from how many touches and how recent.

### 3.4 ManipulationEngine

The market-maker-method core. Detects the sweep-and-reject sequence:

```
1. Price trades beyond a liquidity pool          (the sweep)
2. Price closes back inside within N bars        (the rejection)
3. Displacement in the opposite direction        (the confirmation)
```

Outputs a `quality` score in [0, 1] from: penetration depth relative to ATR, bars spent beyond the level, rejection candle body ratio, displacement magnitude relative to ATR, and whether the sweep occurred at a session-relevant time.

This stage is where Ashley's existing MMM ruleset is encoded. It must be expressible entirely through config parameters, because every threshold in it will be perturbation-tested in `SPEC-07` §6.

### 3.5 SetupDetector

Emits at most one `Setup` per evaluation. If multiple setups qualify, the highest-priority kind by config order wins, and the others are recorded in `inputs`. Never emit two setups for the same bar.

The `fingerprint` is `sha256(f"{symbol}|{setup_kind}|{direction}|{round(reference_level, digits)}|{structure_break_bar_time}")[:16]`. Duplicate-setup suppression compares fingerprints within a configurable window. This stops the system from taking the same breakout four times as price oscillates around the level.

### 3.6 EvidenceCollector and ConfluenceEngine

Evidence weights come from `StrategyConfig.evidence_weights`. The engine computes:

```
score = sum(e.score for e in evidence) / sum(e.weight for e in evidence) * 10
```

normalised to a 0 to 10 scale so that adding an evidence type does not silently shift the threshold.

Bands from config, with these as the starting point only:

```
score <  6.0   -> WAIT
6.0 to 6.99    -> WEAK
7.0 to 7.99    -> VALID
>= 8.0         -> HIGH
```

**These numbers are placeholders.** They must be derived from research output before any live use, and the promotion gates in `SPEC-07` §7 will reject a strategy version whose thresholds have not been walk-forward validated.

**The correlation guard.** The confluence design fails if evidence types measure the same underlying thing. Build a research report that computes the pairwise correlation of evidence presence across all historical evaluations. Any pair above 0.8 is effectively one signal counted twice, and one of them must be removed or reweighted. Ship this report; do not leave it as a good intention.

### 3.7 GateEvaluator (strategy gates only)

Strategy-level gates evaluated inside the engine:

```
CONFLUENCE_BELOW_THRESHOLD
REGIME_NOT_PERMITTED
SESSION_NOT_PERMITTED
DUPLICATE_SETUP
RR_BELOW_MINIMUM
STOP_DISTANCE_INVALID
NEWS_BLACKOUT
VOLATILITY_OUT_OF_BOUNDS
SPREAD_TOO_WIDE
```

All gates are evaluated even after the first failure. Short-circuiting destroys the telemetry that answers "why is the bot not trading". Cost is negligible.

Execution and risk gates (`DAILY_LOSS_LIMIT`, `AGENT_DISCONNECTED`, `MAX_OPEN_POSITIONS`, etc.) are evaluated outside the engine, in the risk engine and execution guard. They appear on the same `Decision` object, appended by the caller.

### 3.8 TradeConstructor

```
1. entry           = setup.trigger_price, adjusted for entry_mode
                     ('market_on_close', 'limit_at_level', 'stop_beyond_level')
2. structural_stop = setup.invalidation_price
3. atr_stop        = entry -/+ (atr_m15 * config.atr_stop_multiple)
4. stop_loss       = per config.stop_mode:
                     'structural' | 'atr' | 'wider_of' | 'tighter_of'
5. enforce         = broker stops_level_points, and config.min_stop_atr_multiple
6. risk_distance   = abs(entry - stop_loss)
7. take_profits    = per config.tp_mode, from the R-multiple ladder
8. rr              = (first_tp - entry) / risk_distance
```

`config.tp_mode` variants to be compared in research, all implemented behind the same interface:
`fixed_1r`, `fixed_1_5r`, `fixed_2r`, `partial_1r_runner`, `atr_trail`, `structure_trail`, `trend_exit_no_tp`, `2r_then_trail`.

### 3.9 NarrativeGenerator

Produces a deterministic, template-based English explanation. No language model, no randomness. Example output:

> XAUUSD M15 at 2026-09-07 09:15 UTC. Regime EXPANSION, H4 bullish, H1 bullish. Price swept the previous day high at 3416.80 and rejected within 2 bars with a 0.71 body ratio. Structure broke on close above 3417.40. Confluence 8.2 of 10 (HIGH). Long from 3418.20, stop 3412.55 (structural, 1.4 ATR), first target 3428.10 at 1.75R. Passed all gates.

For `WAIT` decisions the narrative leads with the blocking gates. This is what makes the WAIT log readable rather than a wall of JSON.

---

## 4. Strategy configuration

`StrategyConfig` is a Pydantic model serialised to `strategy_versions.config`. Its SHA-256 is the version identity. Changing any value produces a new version; there is no such thing as tweaking a live strategy in place.

Top-level shape:

```yaml
name: tdip
semver: "2.1.0"
symbols: [XAUUSD]
primary_timeframe: M15
context_timeframes: [H1, H4, D1]

context:
  atr_period: 14
  atr_percentile_lookback: 200
  adx_period: 14
  adx_trend_threshold: 25
  compression_atr_percentile: 25
  expansion_atr_percentile: 75
  permitted_regimes: [TRENDING_UP, TRENDING_DOWN, EXPANSION]

structure:
  swing_lookback: 3
  break_on_wick: false
  range_min_bars: 8
  key_levels: [PDH, PDL, PWH, PWL, SESSION_HIGH, SESSION_LOW]

liquidity:
  equal_level_tolerance_atr: 0.15
  min_touches: 2
  lookback_bars: 200

manipulation:
  max_bars_beyond_level: 3
  min_rejection_body_ratio: 0.55
  min_displacement_atr: 0.8
  require_session: [LONDON, LONDON_NY_OVERLAP, NEW_YORK]

setups:
  enabled: [BREAKOUT_RETEST, BREAKOUT, PULLBACK_CONTINUATION]
  retest_max_bars: 6
  retest_tolerance_atr: 0.2
  duplicate_window_bars: 24

evidence_weights:
  HTF_TREND_ALIGNMENT: 2.0
  LIQUIDITY_SWEEP: 2.0
  MANIPULATION_QUALITY: 2.0
  STRUCTURE_BREAK: 2.0
  RETEST_CONFIRMED: 1.5
  SR_ZONE: 1.0
  CANDLE_CONFIRMATION: 1.0
  VOLUME_CONFIRMATION: 1.0
  MOMENTUM_EXPANSION: 1.5
  FIB_GOLDEN_ZONE: 1.0

confluence:
  bands: {weak: 6.0, valid: 7.0, high: 8.0}
  minimum_to_trade: 7.0

trade_construction:
  entry_mode: market_on_close
  stop_mode: wider_of
  atr_stop_multiple: 1.5
  min_stop_atr_multiple: 0.8
  max_stop_atr_multiple: 3.0
  tp_mode: partial_1r_runner
  tp_ladder: [{r: 1.0, fraction: 0.5}, {r: 3.0, fraction: 0.5}]
  breakeven_at_r: 1.0
  breakeven_buffer_atr: 0.1
  trail_mode: atr
  trail_atr_multiple: 2.0
  max_holding_bars: 96

filters:
  sessions: [LONDON, LONDON_NY_OVERLAP, NEW_YORK]
  max_spread_atr_multiple: 0.12
  news_blackout_impacts: [high]
  min_rr: 1.5
```

Every number above is a research parameter. None of them are correct yet. The point of the config surface is that finding the correct ones requires zero code changes.

---

## 5. Indicator implementation rules

1. Indicators live in `engines/indicators/`, are pure functions over `tuple[Bar, ...]`, and return `tuple[Decimal | None, ...]` of the same length with `None` for warm-up periods.
2. Never use a library whose warm-up and smoothing behaviour you have not verified against MT5. Wilder's smoothing for ATR and ADX differs from a simple EMA and the difference compounds. Write both and pin the MT5-matching one with a golden test against exported MT5 indicator values.
3. Every indicator gets a test comparing at least 500 values against an MT5 CSV export for the same symbol and period. Discrepancy tolerance: 1e-8.
4. Indicator results are memoised per `(symbol, timeframe, last_bar_time, params)` in Redis with a short TTL in live mode, and in-process in backtest mode. The memo layer sits outside the engine so the engine stays pure.

---

## 6. What is deliberately not in the engine

- No machine learning. Not in v1. A model would make the determinism and audit story much harder, and there is no edge to model until the rule-based version establishes one exists.
- No parameter adaptation at runtime. A strategy that changes its own parameters cannot be walk-forward validated, because the thing you validated is not the thing that ran.
- No position sizing. That is the risk engine's exclusive responsibility.
- No knowledge of orders, brokers, fills or slippage. The engine produces a price-level decision; the execution layer deals with reality.
