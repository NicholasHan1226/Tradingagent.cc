# Market Evidence & Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show sourced multi-market coverage truth and explicit opportunity-to-position/PnL attribution in the read-only terminal.

**Architecture:** The SharedSignals reader returns a cached pulse result plus per-market coverage diagnostics. Snapshot parsers preserve only explicit correlation IDs and numeric PnL components; a pure linked-context view model joins them strictly by equal opportunity ID.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Vite, Node snapshot API.

## Global Constraints

- Keep the frontend and snapshot API read-only.
- Do not add dependencies or source-symbol guesses.
- Missing source data renders `—`, not synthetic zeroes or prices.
- Preserve 900ms source timeout and 15-second cache.
- Desktop targets are 1280x720 and 1440x900; mobile remains deferred.

---

### Task 1: Bounded market coverage read model

**Files:**
- Modify: `front/src/types/dashboard.ts`
- Modify: `front/src/server/sharedSignalsMarketPulse.ts`
- Modify: `front/src/server/sharedSignalsMarketPulse.test.ts`
- Modify: `front/src/api/tradingAgentReadModel.ts`
- Modify: `front/src/server/tradingAgentSnapshot.ts`

**Interfaces:**
- `readSharedSignalsMarketPulses(...)` returns `{ pulses, coverage }`.
- `coverage.entries` has all six markets and only uses symbols found in holdings/signals.

- [ ] Write failing tests for no-representative, unavailable, degraded, sourced, and cached coverage states.
- [ ] Run `npm test -- --run src/server/sharedSignalsMarketPulse.test.ts` and confirm failure because coverage is absent.
- [ ] Implement the typed result, bounded status collection, cache provenance, and optional snapshot field.
- [ ] Re-run the focused reader and snapshot tests.

### Task 2: Explicit PnL attribution

**Files:**
- Modify: `front/src/types/dashboard.ts`
- Modify: `front/src/server/tradingAgentSnapshot.ts`
- Modify: `front/src/server/tradingAgentSnapshot.test.ts`
- Modify: `front/src/lib/linkedEvidenceContext.ts`
- Modify: `front/src/lib/linkedEvidenceContext.test.ts`

**Interfaces:**
- `HoldingRow` can carry explicit `opportunityId`, `realizedPnl`, and `unrealizedPnl`.
- `createLinkedEvidenceContext(events, opportunityId, signals, holdings)` reports explicit-only signal/holding/PnL facts.

- [ ] Write failing parser and view-model tests for equal-ID attribution and mismatched-symbol non-attribution.
- [ ] Run the focused tests and confirm failure because position metadata is discarded.
- [ ] Preserve explicit source fields and calculate only linked PnL facts.
- [ ] Re-run the focused tests.

### Task 3: Terminal evidence surfaces and documentation

**Files:**
- Modify: `front/src/lib/marketTapeViewModel.ts`
- Modify: `front/src/lib/marketTapeViewModel.test.ts`
- Modify: `front/src/components/terminal/MarketTape.tsx`
- Modify: `front/src/components/terminal/LinkedEvidenceContext.tsx`
- Modify: `front/src/components/terminal/TerminalComponents.test.tsx`
- Modify: `front/src/App.tsx`
- Modify: `front/src/App.css`
- Modify: `front/DESIGN.md`
- Modify: `front/README.md`
- Modify: `front/docs/integration.md`
- Modify: `TradingAgent/STATUS.md`

**Interfaces:**
- `createMarketPulseHealth` converts coverage to compact diagnostic copy.
- `MarketTape` accepts optional health; `LinkedEvidenceContext` renders attribution facts from its model.

- [ ] Write failing view/component tests for coverage copy and honest empty attribution.
- [ ] Run focused UI tests and confirm failure because the new evidence is not rendered.
- [ ] Implement compact terminal composition and document contract boundaries.
- [ ] Run lint, all tests, both builds, desktop visual QA, release preflight, review diff, commit, push, deploy, and verify local/main/production-source/runtime/public layers separately.
