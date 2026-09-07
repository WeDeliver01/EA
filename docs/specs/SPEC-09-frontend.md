# SPEC-09: Terminal (Next.js)

Built last, in Phase 7. The system must be fully operable through the API and logs before a pixel is drawn, so that the UI is never load-bearing for safety.

---

## 1. Stack

- Next.js 15, App Router, TypeScript strict
- Tailwind CSS
- TanStack Query for server state, with a single WebSocket client pushing invalidations
- Zustand for local UI state only
- TradingView Lightweight Charts
- Zod schemas generated from the backend OpenAPI spec, checked in CI so a contract change breaks the build

No component library beyond headless primitives (Radix). The terminal is dense and specific; a general-purpose kit fights it.

---

## 2. Design direction

Dark, neutral, high information density. No gradients. No decorative colour.

```
Background      #0B0D0F
Surface         #14171A
Surface raised  #1C2025
Border          #262B31
Text primary    #E8EAED
Text secondary  #8B9299
Text muted      #5A6169

Long / profit   #16A34A
Short / loss    #DC2626
Warning         #F4C430
Neutral accent  #3B82F6
Blocked / off   #5A6169
```

Colour carries one meaning only: direction and P/L. Nothing else is coloured. A green number always means profit, never "success" in a different sense.

Typography: a monospace face for every number (Geist Mono or JetBrains Mono), a sans face for prose. Prices, volumes and R multiples align on the decimal, always, everywhere. Tabular figures are non-negotiable in a trading interface.

Numbers are never abbreviated in the trading views. `R1,240.50`, not `R1.2k`.

---

## 3. Routes

```
/                       Dashboard
/positions              Open positions and management
/signals                Signal and decision feed, including WAIT
/trades                 Closed trade history and analytics
/analysis/[id]          Single decision detail: evidence, gates, snapshot, chart
/strategy               Versions, configs, assignments, diff view
/research               Backtest runs, comparison, reports
/research/[id]          Full validation report
/risk                   Risk profile versions, limits, risk events
/system                 Agent status, workers, discrepancies, system events
/audit                  Audit log
/settings               Account, TOTP, notifications
```

---

## 4. Dashboard

Fixed layout, no drag-and-drop, no customisation. It should look identical every time so that a change is a signal.

**Status bar, always visible, top of every page:**

```
[ AGENT ● 84ms ]  [ TRADING: ENABLED ]  [ KILL SWITCH: OFF ]
[ RECON: CLEAN ]  [ EQUITY R252,840 ]  [ TODAY +R2,840 ]  [ DD 3.4% ]
```

Each chip is red when it is a problem, neutral when fine. `RECON: CLEAN` turning red is the single most important pixel in the application.

**Panels:**

1. **Account** - balance, equity, today's realised and unrealised, open risk as a percentage, drawdown from peak, trades today against the limit.
2. **Open positions** - one row each: symbol, direction, volume, entry, current, SL, TP, R now, unrealised P/L, holding time, strategy version, confluence at entry, and inline actions (breakeven, partial close, close, modify).
3. **Latest decision** - the most recent evaluation per active market, `TRADE` or `WAIT`, the score, and the blocking gates when it was a WAIT. This panel is why the system feels alive when it is not trading.
4. **Gate telemetry** - a 7 day bar chart of rejections by code. Answers "why has nothing happened" without opening a terminal.
5. **Controls** - kill switch, close all, both behind the confirmations in `SPEC-03` §4.
6. **Alerts** - unacknowledged risk events and discrepancies.

---

## 5. Decision detail view

The most valuable page in the product, and the one worth over-investing in.

```
Header      symbol, timeframe, timestamp, outcome, score, strategy version
Chart       M15 candles around the decision, with the HTF bias band, the key
            levels, the liquidity pool that was swept, the structure break,
            entry, stop, TP levels, and the actual fill and exit if it traded
Narrative   the generated English explanation, verbatim
Evidence    each type, present or absent, weight, contribution, and the detail
            payload expandable
Gates       every gate, pass or fail, with the values that decided it
Snapshot    the raw MarketState, collapsible, with a "replay" button that calls
            the replay endpoint and shows the diff
Outcome     if it traded: the linked intent, orders, deals, position, trade,
            MAE, MFE, slippage
```

The replay button turning up a diff means the engine is no longer deterministic. Make that failure loud and unmissable.

---

## 6. Real-time behaviour

One WebSocket connection per browser session, opened at layout level. Incoming events invalidate the relevant TanStack Query keys rather than mutating the cache directly, except for high-frequency quote and P/L updates which write straight to a Zustand store to avoid re-render storms.

Reconnection: exponential backoff, a visible "reconnecting" state in the status bar, and a `resume` frame on reconnect. While disconnected, the UI dims live numbers and shows their age. It must never display a stale price as if it were current. That is how a person makes a decision on information that is thirty seconds old.

---

## 7. Control safety in the UI

- Destructive actions use a typed confirmation, never a plain "are you sure".
- `Close all` requires typing the account label exactly.
- Live-account controls prompt for a TOTP code inline.
- Every control action shows an optimistic pending state and then the server's actual result. Never show success before the server confirms it.
- The kill switch's two states are visually unambiguous at a glance from across a room. This is the one place where a large, obvious control beats a dense one.

---

## 8. What the terminal must not do

- It must not compute anything the backend computes. No P/L maths, no R multiples, no risk percentages calculated client-side. The backend is the single source of truth and a second implementation will drift.
- It must not be required for the system to trade safely. Everything it does is available through the API, and the system runs correctly with no browser open.
- It must not hide a failure state to look tidy. A disconnected agent, an unresolved discrepancy or a stalled worker is displayed prominently, not tucked into a settings page.
