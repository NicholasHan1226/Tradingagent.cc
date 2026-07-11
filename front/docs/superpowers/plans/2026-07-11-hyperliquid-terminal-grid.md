# Hyperliquid Terminal Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace secondary-page SaaS dashboard layouts with one Hyperliquid-style read-only terminal grid and dense automated-process, portfolio and risk ledgers.

**Architecture:** Add pure terminal view-model helpers for currency, exposure, process fallback and risk rows. Render them through reusable TerminalPageShell, ProcessBook, PortfolioLedger and RiskLedger components. Keep the existing API and runtime boundaries unchanged.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, Testing Library, Recharts, CSS tokens.

## Global Constraints

- Desktop only: 1280×720 and 1440×900.
- No backend contract, queue, order, capital, strategy, cron or real-money changes.
- Test-first for every new behavior.
- Missing facts render as `—`; no synthetic trading facts.
- Hyperliquid reference was inspected as a local, temporary design-audit artifact; it is not a repository or production dependency. Durable comparison results are recorded in `front/design-qa.md`.

---

### Task 1: Terminal View Models

**Files:**
- Create: `src/lib/terminalViewModels.ts`
- Create: `src/lib/terminalViewModels.test.ts`

**Interfaces:**
- Produces `createProcessBookRows`, `createPortfolioLedgerRows`, `createRiskLedgerRows`, `summarizePortfolioCurrency`.

- [ ] Write failing tests for completed fallback, CNY holdings total, mixed currency, duplicate asset names, derived weights and missing facts.
- [ ] Run `npm test -- --run src/lib/terminalViewModels.test.ts` and confirm failure.
- [ ] Implement pure helpers using existing `SignalRow`, `HoldingRow`, `PortfolioSummary` and formatting utilities.
- [ ] Re-run the focused test and commit `feat: model terminal ledgers`.

### Task 2: Shared Terminal Components

**Files:**
- Create: `src/components/terminal/TerminalPageShell.tsx`
- Create: `src/components/terminal/TerminalMetricStrip.tsx`
- Create: `src/components/terminal/ProcessBook.tsx`
- Create: `src/components/terminal/PortfolioLedger.tsx`
- Create: `src/components/terminal/RiskLedger.tsx`
- Create: `src/components/terminal/TerminalComponents.test.tsx`

**Interfaces:**
- `TerminalPageShell({ metrics, primary, inspector, ledger? })`
- Ledgers consume rows from Task 1 only.

- [ ] Write failing rendering/accessibility tests for all components and exact column headers.
- [ ] Run the focused test and confirm failure.
- [ ] Implement semantic terminal tables, inspector slots and compact metrics.
- [ ] Re-run the focused test and commit `feat: add terminal ledger components`.

### Task 3: Overview Fallback and Workbench Density

**Files:**
- Modify: `src/components/workbench/WorkbenchBlotter.tsx`
- Modify: `src/components/workbench/WorkbenchBlotter.test.tsx`
- Modify: `src/components/workbench/WorkbenchShell.tsx`
- Modify: `src/pages/HomeDashboard.tsx`

**Interfaces:**
- When `active.length === 0 && completed.length > 0`, uncontrolled blotter defaults to `completed` and controlled initialization receives the same preferred tab.

- [ ] Add a failing test proving `已完成` is selected when running is empty.
- [ ] Implement preferred-tab derivation without overriding a user-selected tab after mount.
- [ ] Replace the large empty running panel with ProcessBook fallback behavior.
- [ ] Run workbench and App tests; commit `refactor: densify automated workbench`.

### Task 4: Rebuild Theme Pages

**Files:**
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/App.test.tsx`
- Modify: `src/components/charts/RiskTimeline.tsx`
- Delete rendering use of: `PageSummaryBoard`, `AllocationPanel`, `ResultSummary`, `RiskSnapshot`, `OpportunityFocus` from theme pages.

**Interfaces:**
- Every theme page renders `TerminalPageShell`.
- Process uses ProcessBook; holdings uses PortfolioLedger; risk uses RiskLedger; review uses completed/review tabs.

- [ ] Update App tests first to require terminal shells and reject old summary-card surfaces.
- [ ] Run App tests and confirm failure.
- [ ] Recompose each theme page with the new terminal components and chart/inspector content.
- [ ] Add limit/warning bands to RiskTimeline using the existing 7% boundary constant.
- [ ] Run App and chart tests; commit `refactor: unify theme pages as terminal grids`.

### Task 5: Terminal Visual System and Copy

**Files:**
- Modify: `src/App.css`
- Modify: `src/index.css`
- Modify: `src/components/tables/SignalTable.tsx`
- Modify: `src/components/tables/HoldingsTable.tsx`
- Modify: `DESIGN.md`
- Modify: `README.md`

**Interfaces:**
- CSS classes: `.terminal-page-shell`, `.terminal-metric-strip`, `.terminal-primary`, `.terminal-inspector`, `.terminal-ledger`, `.exposure-bar`, `.risk-band`.

- [ ] Raise secondary UI typography to 12px and strengthen muted contrast.
- [ ] Remove theme-page hero/card geometry, oversized donut, wide CTA and dead styles no longer rendered.
- [ ] Rename visible `下次规则` to `自动校准` and remove duplicated asset text.
- [ ] Rewrite design documentation for the six current pages and new component mapping.
- [ ] Run lint/tests/build and commit `style: align full terminal design language`.

### Task 6: Design QA and Release Verification

**Files:**
- Create: `design-qa.md`
- Modify: `STATUS.md` only after production proof.

- [ ] Run `npm run lint`, `npm test -- --run`, `npm run build`, and `npm run build:api`.
- [ ] Run local Vite, capture 1440×900 and 1280×720 overview/process/holdings/risk states, and compare with the Hyperliquid reference.
- [ ] Fix every P0/P1/P2 finding and repeat until `design-qa.md` says `final result: passed`.
- [ ] Merge to local main without overwriting unrelated dirty paths, push GitHub main, preserve rollback builds, fast-forward production and rebuild with Node 24 PATH.
- [ ] Verify source SHA equality, service health, public HTTP/snapshot, browser build id, six-page navigation and absence of execution controls.
- [ ] Record production proof in STATUS and re-align local main, origin/main and production source.
