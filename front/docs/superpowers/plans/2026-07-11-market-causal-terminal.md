# Market-Causal Terminal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sourced market pulse, persistent opportunity correlation and local desktop terminal controls to the existing TradingAgent frontend.

**Architecture:** A bounded server-side SharedSignals reader enriches the existing read-only snapshot. Pure view models map pulses and opportunity context into focused React components, while a versioned local preference module owns density and table columns.

**Tech Stack:** React 19, TypeScript 6, Vitest, Testing Library, Recharts, Vite, Node HTTP snapshot API.

## Global Constraints

- Keep the frontend and snapshot API read-only.
- Do not add dependencies.
- Missing source fields render `—`; never synthesize price movement.
- Desktop targets are 1280×720 and 1440×900; mobile remains deferred.
- Preserve existing page, market, range and keyboard navigation.

---

### Task 1: SharedSignals market pulse

**Files:**
- Create: `front/src/server/sharedSignalsMarketPulse.ts`
- Create: `front/src/server/sharedSignalsMarketPulse.test.ts`
- Modify: `front/src/types/dashboard.ts`
- Modify: `front/src/api/tradingAgentReadModel.ts`
- Modify: `front/src/server/tradingAgentSnapshot.ts`

**Interfaces:**
- Produces `readSharedSignalsMarketPulses({ baseUrl, holdings, signals, fetchImpl }): Promise<MarketPulse[]>`.
- Snapshot adds optional `marketPulses` and source reference `sharedSignalsMarketPulse`.

- [ ] Write tests proving supported symbol routing, OHLCV normalization, point ordering, timeout degradation and cache reuse.
- [ ] Run `npm test -- --run src/server/sharedSignalsMarketPulse.test.ts` and confirm the missing module fails.
- [ ] Implement the bounded reader and snapshot integration.
- [ ] Re-run the focused test and snapshot tests.

### Task 2: Market-native tape

**Files:**
- Create: `front/src/components/terminal/MarketSparkline.tsx`
- Modify: `front/src/lib/marketTapeViewModel.ts`
- Modify: `front/src/lib/marketTapeViewModel.test.ts`
- Modify: `front/src/components/terminal/MarketTape.tsx`
- Modify: `front/src/components/terminal/TerminalComponents.test.tsx`
- Modify: `front/src/App.tsx`
- Modify: `front/src/App.css`

**Interfaces:**
- `createMarketTapeRows` accepts `MarketPulse[]` and exposes pulse price, change, freshness and points.
- `MarketSparkline` consumes only real numeric points.

- [ ] Add failing view-model and component assertions for sourced pulse and truthful empty state.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement the sparkline, semantic tokens and denser tape composition.
- [ ] Re-run focused tests.

### Task 3: Opportunity-linked context

**Files:**
- Create: `front/src/lib/linkedEvidenceContext.ts`
- Create: `front/src/lib/linkedEvidenceContext.test.ts`
- Create: `front/src/components/terminal/LinkedEvidenceContext.tsx`
- Modify: `front/src/hooks/useTerminalNavigation.ts`
- Modify: `front/src/hooks/useTerminalNavigation.test.tsx`
- Modify: `front/src/components/terminal/ProcessCycleLedger.tsx`
- Modify: `front/src/components/terminal/ProcessEventLedger.tsx`
- Modify: `front/src/pages/ThemePage.tsx`
- Modify: `front/src/App.tsx`

**Interfaces:**
- URL navigation adds optional `opportunity`.
- `createLinkedEvidenceContext(events, opportunityId)` returns sourced display context or `null`.
- Process cycles emit `onSelect(row.id)` and expose selected state.

- [ ] Add failing tests for URL restore, context resolution and event filtering.
- [ ] Run the focused tests and confirm failures.
- [ ] Implement selection, URL persistence, context bar and filtered raw ledger.
- [ ] Re-run focused tests.

### Task 4: Desktop command and preferences

**Files:**
- Create: `front/src/lib/terminalPreferences.ts`
- Create: `front/src/lib/terminalPreferences.test.ts`
- Create: `front/src/components/terminal/TerminalCommandPalette.tsx`
- Create: `front/src/components/terminal/TerminalCommandPalette.test.tsx`
- Modify: `front/src/components/terminal/TerminalDataTable.tsx`
- Modify: `front/src/components/terminal/TerminalTableToolbar.tsx`
- Modify: `front/src/App.tsx`
- Modify: `front/src/App.css`

**Interfaces:**
- Versioned `TerminalPreferences` stores density and table column keys.
- `TerminalCommandPalette` receives commands and emits the selected command ID.

- [ ] Add failing tests for preference migration/fallback and keyboard command execution.
- [ ] Run focused tests and confirm failures.
- [ ] Implement local preferences, density control, persisted columns and command palette.
- [ ] Re-run focused tests.

### Task 5: Documentation and release verification

**Files:**
- Modify: `front/DESIGN.md`
- Modify: `front/README.md`
- Modify: `front/docs/integration.md`
- Modify: `front/src/App.tsx`

- [ ] Update the design language, data contract, local preference boundary and build marker.
- [ ] Run `npm run lint`, `npm test -- --run`, `npm run build`, and `npm run build:api`.
- [ ] Validate 1280×720 and 1440×900 in a real browser, including cycle selection, command palette, density and overflow.
- [ ] Review the final diff, run safe release preflight, commit, push, deploy with the documented production path, and verify source/runtime/public layers separately.

