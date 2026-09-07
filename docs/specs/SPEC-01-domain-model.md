# SPEC-01: Domain Model

All types in this document live under `backend/app/domain/`. This package imports nothing else from the project. It has no ORM, no HTTP, no clock, no logging side effects. Everything is a frozen dataclass or a `StrEnum`.

Use `@dataclass(frozen=True, slots=True)`. Use `Decimal` for anything monetary or volume-related.

---

## 1. Enums

```python
# domain/market/enums.py
class Timeframe(StrEnum):
    M1 = "M1"; M5 = "M5"; M15 = "M15"; M30 = "M30"
    H1 = "H1"; H4 = "H4"; D1 = "D1"; W1 = "W1"

    @property
    def seconds(self) -> int: ...

class AssetClass(StrEnum):
    FX = "FX"; METAL = "METAL"; INDEX = "INDEX"; CRYPTO = "CRYPTO"; ENERGY = "ENERGY"

class Direction(StrEnum):
    LONG = "LONG"; SHORT = "SHORT"

class Session(StrEnum):
    SYDNEY = "SYDNEY"; TOKYO = "TOKYO"; LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"; LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"; DEAD = "DEAD"

class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    COMPRESSION = "COMPRESSION"        # low ATR percentile, tight range
    EXPANSION = "EXPANSION"            # ATR breaking out of its own range
    RANGING = "RANGING"
    CHOPPY = "CHOPPY"                  # conflicting structure, no trade
    UNKNOWN = "UNKNOWN"                # insufficient data, no trade
```

```python
# domain/strategy/enums.py
class DecisionOutcome(StrEnum):
    TRADE = "TRADE"
    WAIT = "WAIT"

class EvidenceType(StrEnum):
    HTF_TREND_ALIGNMENT = "HTF_TREND_ALIGNMENT"
    LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
    STRUCTURE_BREAK = "STRUCTURE_BREAK"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    SR_ZONE = "SR_ZONE"
    FIB_GOLDEN_ZONE = "FIB_GOLDEN_ZONE"
    CANDLE_CONFIRMATION = "CANDLE_CONFIRMATION"
    VOLUME_CONFIRMATION = "VOLUME_CONFIRMATION"
    MANIPULATION_QUALITY = "MANIPULATION_QUALITY"
    MOMENTUM_EXPANSION = "MOMENTUM_EXPANSION"
    VOLATILITY_REGIME = "VOLATILITY_REGIME"
    SESSION_QUALITY = "SESSION_QUALITY"

class GateCode(StrEnum):
    # market and connectivity
    MARKET_CLOSED = "MARKET_CLOSED"
    PRICE_STALE = "PRICE_STALE"
    AGENT_DISCONNECTED = "AGENT_DISCONNECTED"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"
    # cost and quality
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    VOLATILITY_OUT_OF_BOUNDS = "VOLATILITY_OUT_OF_BOUNDS"
    SESSION_NOT_PERMITTED = "SESSION_NOT_PERMITTED"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"
    # risk
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    MAX_DAILY_TRADES = "MAX_DAILY_TRADES"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_OPEN_RISK = "MAX_OPEN_RISK"
    CORRELATED_EXPOSURE = "CORRELATED_EXPOSURE"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    # setup quality
    RR_BELOW_MINIMUM = "RR_BELOW_MINIMUM"
    STOP_DISTANCE_INVALID = "STOP_DISTANCE_INVALID"
    CONFLUENCE_BELOW_THRESHOLD = "CONFLUENCE_BELOW_THRESHOLD"
    DUPLICATE_SETUP = "DUPLICATE_SETUP"
    REGIME_NOT_PERMITTED = "REGIME_NOT_PERMITTED"
    # control
    TRADING_DISABLED = "TRADING_DISABLED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    STRATEGY_PAUSED = "STRATEGY_PAUSED"
```

```python
# domain/execution/enums.py
class OrderType(StrEnum):
    MARKET = "MARKET"; LIMIT = "LIMIT"; STOP = "STOP"

class OrderSide(StrEnum):
    BUY = "BUY"; SELL = "SELL"

class ExecutionState(StrEnum):
    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    # terminal failures
    RISK_BLOCKED = "RISK_BLOCKED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    BROKER_ERROR = "BROKER_ERROR"
    UNKNOWN = "UNKNOWN"          # requires manual or reconciliation resolution

class DealType(StrEnum):
    ENTRY = "ENTRY"; EXIT = "EXIT"; PARTIAL_EXIT = "PARTIAL_EXIT"
    SWAP = "SWAP"; COMMISSION = "COMMISSION"; CORRECTION = "CORRECTION"

class PositionStatus(StrEnum):
    OPEN = "OPEN"; CLOSED = "CLOSED"; ORPHANED = "ORPHANED"
    # ORPHANED: exists at broker, not attributable to any intent
```

---

## 2. Market value objects

```python
# domain/market/bar.py
@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: Timeframe
    open_time: datetime      # UTC, always the bar OPEN
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int
    real_volume: int | None
    spread_points: int | None

    @property
    def close_time(self) -> datetime: ...
    @property
    def body(self) -> Decimal: ...
    @property
    def range(self) -> Decimal: ...
    @property
    def body_ratio(self) -> Decimal: ...   # abs(body) / range, 0 if range == 0
```

**Bar convention, stated once and never violated:** `open_time` is the bar's opening timestamp in UTC. A bar is *closed* and eligible for analysis only when `now >= open_time + timeframe.seconds + CANDLE_CLOSE_GRACE_MS`. The strategy engine never sees a forming bar. Violating this is the single most common source of a backtest that cannot be reproduced live.

```python
# domain/market/quote.py
@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    server_time: datetime     # broker time, converted to UTC
    received_at: datetime     # our clock, UTC

    @property
    def spread(self) -> Decimal: ...
    @property
    def mid(self) -> Decimal: ...

# domain/market/symbol_spec.py
@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Broker contract specification. Fetched from MT5, never guessed."""
    symbol: str
    asset_class: AssetClass
    digits: int
    point: Decimal                # smallest price increment, e.g. 0.01 for XAUUSD
    tick_size: Decimal
    tick_value: Decimal           # account currency value of one tick per 1.0 lot
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int       # broker minimum SL/TP distance from price
    freeze_level_points: int
    margin_initial: Decimal
    currency_profit: str
    currency_margin: str
    quote_currency: str
```

`tick_value` and `contract_size` are pulled from `mt5.symbol_info()` on every agent connect and written to `instruments`. Position sizing that hard-codes pip values is a bug.

```python
# domain/market/market_state.py
@dataclass(frozen=True, slots=True)
class MarketState:
    """The COMPLETE input to a strategy evaluation. Nothing else may be read."""
    symbol: str
    spec: SymbolSpec
    as_of: datetime                              # the close time of the primary bar
    primary_tf: Timeframe
    bars: Mapping[Timeframe, tuple[Bar, ...]]    # oldest first, closed bars only
    quote: Quote
    session: Session
    account: AccountState
    open_positions: tuple[Position, ...]
    recent_signals: tuple[SignalRef, ...]        # for duplicate-setup detection
    calendar_events: tuple[CalendarEvent, ...]   # within the blackout window
    risk_state: RiskState
```

Constructing `MarketState` is the job of the market data engine. The backtester constructs the identical object from historical data. That equivalence is the whole design.

---

## 3. Strategy value objects

```python
# domain/strategy/evidence.py
@dataclass(frozen=True, slots=True)
class Evidence:
    type: EvidenceType
    direction: Direction | None
    present: bool
    weight: Decimal            # from the strategy version config
    score: Decimal             # weight if present else 0
    detail: Mapping[str, Any]  # serialised to JSONB, must be JSON-safe

# domain/strategy/gate_result.py
@dataclass(frozen=True, slots=True)
class GateResult:
    code: GateCode
    passed: bool
    detail: Mapping[str, Any]

# domain/strategy/setup.py
@dataclass(frozen=True, slots=True)
class Setup:
    kind: str                  # "BREAKOUT", "BREAKOUT_RETEST", "PULLBACK_CONTINUATION"
    direction: Direction
    trigger_price: Decimal
    invalidation_price: Decimal    # structural stop level, before ATR adjustment
    reference_level: Decimal       # the level that was broken or retested
    fingerprint: str               # deterministic hash for duplicate detection

# domain/strategy/decision.py
@dataclass(frozen=True, slots=True)
class Decision:
    outcome: DecisionOutcome
    symbol: str
    as_of: datetime
    strategy_version_id: UUID
    regime: Regime
    setup: Setup | None
    direction: Direction | None
    entry: Decimal | None
    stop_loss: Decimal | None
    take_profits: tuple[TakeProfit, ...]
    confluence_score: Decimal
    confluence_band: str            # WAIT | WEAK | VALID | HIGH
    evidence: tuple[Evidence, ...]
    gates: tuple[GateResult, ...]
    narrative: str                  # human-readable explanation, generated
    engine_duration_ms: int

    @property
    def blocking_gates(self) -> tuple[GateResult, ...]: ...

@dataclass(frozen=True, slots=True)
class TakeProfit:
    level: Decimal
    fraction: Decimal        # portion of the position to close, sums to <= 1
    r_multiple: Decimal
```

A `Decision` is always produced, even when the outcome is `WAIT`. Every evaluation writes an `analysis_run` row and a `decision` row. This is P3.

---

## 4. Risk value objects

```python
# domain/risk/state.py
@dataclass(frozen=True, slots=True)
class RiskState:
    as_of: datetime
    realised_pnl_today: Decimal
    realised_pnl_week: Decimal
    open_risk: Decimal              # sum of (entry - stop) * size across open positions
    trades_today: int
    open_position_count: int
    consecutive_losses: int
    peak_equity: Decimal
    current_drawdown_pct: Decimal
    trading_enabled: bool
    kill_switch_active: bool

@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: Decimal          # e.g. 0.005 = 0.5%
    max_daily_loss_pct: Decimal
    max_weekly_loss_pct: Decimal
    max_open_risk_pct: Decimal
    max_daily_trades: int
    max_open_positions: int
    max_positions_per_symbol: int
    max_correlated_positions: int
    min_rr: Decimal
    max_spread_multiple_of_atr: Decimal
    pause_after_consecutive_losses: int
    pause_duration_minutes: int
    max_lot_size: Decimal

@dataclass(frozen=True, slots=True)
class PositionSize:
    volume: Decimal                 # rounded to volume_step
    risk_amount: Decimal            # actual account-currency risk after rounding
    risk_pct_actual: Decimal
    stop_distance_price: Decimal
    stop_distance_points: int
    value_per_point_per_lot: Decimal
    margin_required: Decimal
    calculation: Mapping[str, Any]  # every intermediate value, for audit

@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    size: PositionSize | None
    gates: tuple[GateResult, ...]
    limits_snapshot: RiskLimits
    state_snapshot: RiskState
```

---

## 5. Execution value objects

```python
# domain/execution/intent.py
@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str        # ULID. Generated ONCE, before any network call.
    signal_id: UUID
    account_id: UUID
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    limit_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal | None      # first TP; ladder is managed post-fill
    max_slippage_points: int
    magic: int                       # strategy version fingerprint, see SPEC-06 §7
    comment: str                     # first 24 chars of client_order_id
    expires_at: datetime | None
    idempotency_key: str             # = client_order_id, restated for clarity

@dataclass(frozen=True, slots=True)
class Fill:
    broker_deal_id: str
    client_order_id: str
    broker_order_id: str
    broker_position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    executed_at: datetime           # broker server time, converted to UTC
    deal_type: DealType

@dataclass(frozen=True, slots=True)
class Position:
    id: UUID
    broker_position_id: str
    account_id: UUID
    symbol: str
    direction: Direction
    volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    opened_at: datetime
    status: PositionStatus
    signal_id: UUID | None          # None means ORPHANED
    initial_risk: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    breakeven_moved: bool
    partials_taken: int
```

---

## 6. Execution state machine

This is the authoritative transition table. Any transition not listed here is a bug and must raise `IllegalStateTransition`.

```
DETECTED         -> VALIDATING, RISK_BLOCKED
VALIDATING       -> APPROVED, RISK_BLOCKED, REJECTED
APPROVED         -> QUEUED, RISK_BLOCKED, EXPIRED
QUEUED           -> SENT, CANCELLED, EXPIRED
SENT             -> ACKNOWLEDGED, REJECTED, BROKER_ERROR, UNKNOWN
ACKNOWLEDGED     -> PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, BROKER_ERROR
PARTIALLY_FILLED -> PARTIALLY_FILLED, FILLED, CANCELLED
FILLED           -> POSITION_OPEN
POSITION_OPEN    -> CLOSING, CLOSED
CLOSING          -> CLOSED, POSITION_OPEN        (close attempt failed, still open)
UNKNOWN          -> ACKNOWLEDGED, FILLED, REJECTED, CANCELLED, BROKER_ERROR
                                                 (resolved only by reconciliation)

Terminal: CLOSED, RISK_BLOCKED, REJECTED, CANCELLED, EXPIRED, BROKER_ERROR
```

Rules:

1. `SENT` is written to the database **before** the network call, in the same transaction as the outbox row. If the process dies mid-call, recovery finds a `SENT` intent and moves it to `UNKNOWN`.
2. `UNKNOWN` is not a failure. It is a state that requires evidence. The reconciliation worker resolves it by querying the broker for the `client_order_id` in the order comment, the magic number, and a symbol and time window. See `SPEC-06` §6.
3. Nothing may transition out of a terminal state.
4. Every transition writes a row to `execution_transitions` with the reason, the actor (`worker`, `reconciler`, `user`, `agent`), and a correlation id.

Implement as:

```python
_ALLOWED: Mapping[ExecutionState, frozenset[ExecutionState]] = {...}

def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in _ALLOWED.get(current, frozenset()):
        raise IllegalStateTransition(current, target)
```

Property test required: from every state, attempting every disallowed target raises.

---

## 7. Position lifecycle (separate from the execution state machine)

Once `POSITION_OPEN`, the position manager owns the position and applies, in this order, on every evaluation tick:

```
1. Hard stop present at broker?          if not, set it immediately, alert
2. Emergency exit conditions?            spread blowout, agent loss beyond
                                         grace period, kill switch + close-all
3. Stop loss hit?                        broker handles; we only detect
4. Partial take profit due?              close fraction, record deal
5. Breakeven trigger reached?            move SL to entry +/- buffer, once only
6. Trailing active?                      ATR or structure trail, monotonic only
7. Time-based exit?                      max holding period, session end
8. Structural invalidation?              opposite structure break on primary TF
```

Two invariants:

- **The stop loss never moves against the position.** A trailing calculation that would widen the stop is discarded, not applied.
- **The broker always holds a real stop.** The system does not rely on the position monitor to exit. If the Linux side dies, the broker-side SL is what protects the account.

---

## 8. Account state

```python
@dataclass(frozen=True, slots=True)
class AccountState:
    account_id: UUID
    broker: str
    login: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal | None
    leverage: int
    server_time: datetime
    reported_at: datetime
    is_stale: bool          # reported_at older than ACCOUNT_STALE_SECONDS
```

`is_stale` is computed at construction from the injected clock, not read inside the engine. If `is_stale` is true, the `PRICE_STALE` gate fails and no trade is possible.

---

## 9. Identifier conventions

| Entity | Format | Example |
|---|---|---|
| Internal ids | UUIDv7 (time-ordered) | `018f3a2b-...` |
| `client_order_id` | ULID, 26 chars | `01JCXG4Q7T8N2M9RZKWDVYE3PA` |
| Signal reference | `SIG-{YYYYMMDD}-{SYMBOL}-{TF}-{seq}` | `SIG-20260907-XAUUSD-M15-000183` |
| Correlation id | UUIDv4 per request or per analysis run | propagated through logs, streams and agent frames |
| Strategy version | `{name}@{semver}+{config_sha256[:12]}` | `tdip@2.1.0+9f3ac41b7de2` |

`client_order_id` is the idempotency key everywhere. It is generated by the execution worker before the outbox row is written, and it is what the reconciler searches for at the broker.
