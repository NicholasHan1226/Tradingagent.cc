# Explicit Market Attribution & Coverage Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely activate exact cross-market pulse symbols, expose single-origin A-share attribution, and show a bounded coverage observation trace.

**Architecture:** Snapshot parsing carries explicit market-data identifiers only. The pulse reader retains a bounded in-memory history of fresh source results. The local A-share ledger preserves an order ID only when its remaining position has one source order, which the existing read-only snapshot maps to opportunity context.

**Tech Stack:** TypeScript, React 19, Vitest, Python 3, unittest, Vite, Node snapshot API.

## Global Constraints

- No provider, queue, account, capital, cron, order, or callback behavior changes.
- Non-A-share market pulse reads require `market_data_symbol` or `marketDataSymbol`; never derive a code from display symbol.
- Multi-origin open positions omit attribution fields.
- Coverage history is in-process, capped, and resets honestly on service restart.
- Desktop targets remain 1280x720 and 1440x900.

---

### Task 1: Explicit market-data symbol and coverage trace

**Files:**
- Modify: `front/src/types/dashboard.ts`
- Modify: `front/src/server/tradingAgentSnapshot.ts`
- Modify: `front/src/server/tradingAgentSnapshot.test.ts`
- Modify: `front/src/server/sharedSignalsMarketPulse.ts`
- Modify: `front/src/server/sharedSignalsMarketPulse.test.ts`
- Modify: `front/src/api/tradingAgentReadModel.ts`

**Interfaces:**
- `SignalRow.marketDataSymbol` and `HoldingRow.marketDataSymbol` are optional explicit strings.
- `MarketPulseCoverageHistory` contains at most 12 fresh observations.

- [x] Write failing reader tests: a Crypto display symbol without `marketDataSymbol` is unmapped; `BTCUSDT` routes to `/crypto?symbol=BTCUSDT`; two fresh reads create history while a cache hit does not.
- [x] Run `npm test -- --run src/server/sharedSignalsMarketPulse.test.ts src/server/tradingAgentSnapshot.test.ts` and confirm the expected failures.
- [x] Parse explicit fields, select strict representatives, retain fresh-only bounded observations, and attach optional history to snapshot.
- [x] Re-run the focused TypeScript tests.

### Task 2: Single-origin A-share position provenance

**Files:**
- Modify: `shared/execution/local_sim_ledger.py:445-535`
- Modify: `tests/test_local_sim_ledger.py`
- Modify: `front/src/server/tradingAgentSnapshot.test.ts`

**Interfaces:**
- `_replay_account(...)['positions'][symbol]` contains `order_id` only when all outstanding buys for that symbol share one order ID.

- [x] Write failing Python tests for one open order retaining `order_id` and two open orders omitting it.
- [x] Run `python3 -m unittest tests.test_local_sim_ledger` and confirm both new assertions fail.
- [x] Track open order IDs during replay and emit a field only for a unique recorded buy origin; preserve all existing cash/PnL calculations.
- [x] Re-run Python tests and TypeScript snapshot parsing tests.

### Task 3: Compact terminal observation surface and release

**Files:**
- Modify: `front/src/lib/marketTapeViewModel.ts`
- Modify: `front/src/lib/marketTapeViewModel.test.ts`
- Modify: `front/src/components/terminal/EvidenceHealth.tsx`
- Modify: `front/src/components/terminal/TerminalComponents.test.tsx`
- Modify: `front/src/App.tsx`
- Modify: `front/src/App.css`
- Modify: `front/DESIGN.md`, `front/README.md`, `front/docs/integration.md`, `STATUS.md`

**Interfaces:**
- `createMarketPulseHealth` accepts optional coverage history and exposes `traceLabel`/accessible trace detail.

- [x] Write failing view/component tests for the trace label and explicit symbol presentation.
- [x] Run focused UI tests and confirm they fail before the new surface exists.
- [x] Implement the compact tape detail, update contracts, then run lint, all frontend tests, both builds, focused Python tests, desktop QA, release preflight, commit, push, deploy, and separately verify local/main/production source/runtime/public route.
