# Evidence-Adaptive Terminal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make runtime status, returns, holdings and process history adapt to real evidence while preserving the read-only Hyperliquid-style workbench.

**Architecture:** Pure TypeScript view models derive heartbeat, density and opportunity-cycle state from the existing snapshot. Focused React components render those models; no backend mutation or execution contract changes.

**Tech Stack:** React 19, TypeScript 6, Vitest, Testing Library, Recharts, Vite.

## Global Constraints

- Desktop only at 1280x720 and 1440x900; mobile remains deferred.
- No queue/order/account/capital/strategy/cron mutation.
- No fabricated facts; absent evidence renders `—` or explicit waiting copy.
- Preserve the continuous near-black terminal, hairlines, 4px rhythm and semantic cyan/red/amber system.
- Follow red-green-refactor for every behavior change.

---

### Task 1: Runtime heartbeat and presentation translation

**Files:**
- Create: `src/lib/runtimeHeartbeat.ts`
- Create: `src/lib/runtimeHeartbeat.test.ts`
- Create: `src/components/terminal/AutomationHeartbeat.tsx`
- Modify: `src/App.tsx`
- Modify: `src/components/MarketHeader.tsx`
- Modify: `src/pages/ThemePage.tsx`

**Interfaces:**
- Produces `createRuntimeHeartbeat({ domains, generatedAt, marketSummary, signals, funnelEvents, now }): RuntimeHeartbeat`.
- Produces `translateTerminalValue(value): string` for strategy/source/status display values.

- [ ] Write tests proving pending -> live, healthy zero-pending -> idle, aged evidence -> stale, and error/fault -> degraded.
- [ ] Run `npm test -- --run src/lib/runtimeHeartbeat.test.ts` and confirm RED because the module does not exist.
- [ ] Implement the pure model, shared heartbeat component and common translations.
- [ ] Wire header metrics and page inspectors to the same model.
- [ ] Run `npm test -- --run src/lib/runtimeHeartbeat.test.ts src/App.test.tsx src/components/MarketHeader.test.tsx` and confirm GREEN.

### Task 2: Adaptive returns and holdings density

**Files:**
- Create: `src/lib/terminalDensity.ts`
- Create: `src/lib/terminalDensity.test.ts`
- Create: `src/components/terminal/AdaptiveTerminalSurface.tsx`
- Create: `src/components/terminal/EvidenceEmptyState.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/App.css`
- Modify: `src/App.test.tsx`

**Interfaces:**
- Produces `getPerformanceDensity(data, portfolio): 'active' | 'quiet' | 'empty'`.
- Produces `getHoldingsEmptyEvidence({ holdings, signals, portfolio, generatedAt })`.

- [ ] Write failing tests for flat/one-point returns, active returns and empty holdings with a latest closed result.
- [ ] Run `npm test -- --run src/lib/terminalDensity.test.ts src/App.test.tsx` and confirm RED.
- [ ] Implement density models and components, reducing quiet chart height and replacing empty holdings table with sourced evidence.
- [ ] Add semantic density/state tokens and reduced-motion behavior.
- [ ] Run the focused tests and confirm GREEN.

### Task 3: Opportunity cycle ledger

**Files:**
- Create: `src/lib/processCycleViewModel.ts`
- Create: `src/lib/processCycleViewModel.test.ts`
- Create: `src/components/terminal/ProcessCycleLedger.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/components/terminal/TerminalComponents.test.tsx`
- Modify: `src/App.css`

**Interfaces:**
- Produces `createProcessCycles(events: FunnelEvent[]): ProcessCycleRow[]` grouped by opportunity ID and ordered by latest sourced event.

- [ ] Write failing tests for grouping, stage ordering, latency, missing-stage honesty and translation.
- [ ] Run `npm test -- --run src/lib/processCycleViewModel.test.ts src/components/terminal/TerminalComponents.test.tsx` and confirm RED.
- [ ] Implement compact cycle rows and keep `ProcessEventLedger` as the raw audit ledger below.
- [ ] Run focused tests and confirm GREEN.

### Task 4: Documentation, full verification and release

**Files:**
- Modify: `DESIGN.md`
- Modify: `README.md`
- Modify: `design-qa.md`
- Modify: `../STATUS.md`

- [ ] Update the design/data documentation and unchanged read-only boundary.
- [ ] Run `npm run lint`, `npm test -- --reporter=dot`, `npm run build` and `npm run build:api`.
- [ ] Run browser QA on all six pages at 1440x900 and 1280x720; verify state language, no overflow, interactions and console output.
- [ ] Record the eight-part Design Taste scorecard and continue until at least 90/100.
- [ ] Commit only scoped files on `main`, push `origin/main`, follow the documented production backup/build/restart path, then separately verify production commit, service, internal health, public assets and public snapshot.

