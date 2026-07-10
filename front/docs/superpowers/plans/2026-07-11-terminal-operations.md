# Terminal Operations Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every TradingAgent terminal surface state-consistent, evidence-rich, searchable and keyboard-efficient while preserving the read-only execution boundary.

**Architecture:** Pure view-model resolvers own state, market-tape, evidence, event and table derivation. React components consume those models and keep only presentation state. The existing snapshot gains optional holding evidence fields populated only from real source rows.

**Tech Stack:** React 19, TypeScript 6, Vitest, Testing Library, Recharts, Vite, existing Node snapshot server.

## Global Constraints

- Desktop only: 1280×720 and 1440×900.
- No queue writes, orders, account/capital/strategy changes or new mutation endpoints.
- Optional facts render `—`; do not synthesize prices, timestamps or versions.
- Preserve the current neo-industrial Hyperliquid terminal visual system.
- Every behavior change follows red-green-refactor and is committed independently.

---

### Task 1: Authoritative terminal state resolver

**Files:**
- Create: `src/lib/terminalStateResolver.ts`
- Create: `src/lib/terminalStateResolver.test.ts`
- Modify: `src/lib/automationObservatoryViewModel.ts`
- Modify: `src/components/workbench/workbenchBlotterState.ts`
- Modify: `src/components/workbench/WorkbenchShell.tsx`
- Modify: `src/components/workbench/WorkbenchBlotter.test.tsx`

**Interfaces:**
- Produces: `resolveTerminalState({ signals, positions }): TerminalResolvedState`
- Produces: `selectAvailableTab(current, state): BlotterTab`
- `TerminalResolvedState` contains `running`, `completed`, `review`, `runtimeItem`, `preferredTab`, and counts.

- [ ] **Step 1: Write failing resolver tests**

```ts
expect(resolveTerminalState({ signals: [blocked, executed], positions: [] })).toMatchObject({
  running: [],
  preferredTab: 'completed',
  runtimeItem: { kind: 'blocked', contextLabel: '最近事件' },
})
expect(selectAvailableTab('active', resolved)).toBe('completed')
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/lib/terminalStateResolver.test.ts src/components/workbench/WorkbenchBlotter.test.ts`
Expected: FAIL because resolver and post-snapshot tab reconciliation do not exist.

- [ ] **Step 3: Implement resolver and reconciliation**

Categorize only `pending` as running; partial/executed as completed; blocked/missed/cancelled as review. Add a `useEffect` in `WorkbenchShell` that changes the selected tab only when its current dataset is empty.

- [ ] **Step 4: Verify GREEN and regressions**

Run: `npm test -- --run src/lib/terminalStateResolver.test.ts src/lib/automationObservatoryViewModel.test.ts src/components/workbench/WorkbenchBlotter.test.ts src/components/workbench/RuntimeRail.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add front/src/lib/terminalStateResolver* front/src/lib/automationObservatoryViewModel* front/src/components/workbench
git commit -m "fix(front): unify terminal runtime state"
```

### Task 2: Market tape and evidence health

**Files:**
- Create: `src/lib/marketTapeViewModel.ts`
- Create: `src/lib/marketTapeViewModel.test.ts`
- Create: `src/components/terminal/MarketTape.tsx`
- Create: `src/components/terminal/EvidenceHealth.tsx`
- Modify: `src/App.tsx`
- Modify: `src/pages/HomeDashboard.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/App.css`
- Modify: `src/App.test.tsx`

**Interfaces:**
- Produces: `createMarketTapeRows(marketSummaries, activeMarket, generatedAt)`
- Produces: `createEvidenceHealth(domains, generatedAt, marketSummary)`
- `MarketTape` calls the existing `setActiveMarket` only.

- [ ] **Step 1: Write failing view-model and integration tests**

```ts
expect(rows.find(row => row.market === 'A-share')).toMatchObject({ returnLabel: '+1.20%', runtimeLabel: '正常' })
expect(screen.getByRole('navigation', { name: '市场状态带' })).toBeInTheDocument()
expect(screen.getByRole('region', { name: '证据健康' })).toHaveTextContent('快照')
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/lib/marketTapeViewModel.test.ts src/App.test.tsx`
Expected: FAIL because tape and shared evidence health do not exist.

- [ ] **Step 3: Implement pure models and components**

Use `marketSummaries[]`, domain statuses and generated timestamps only. Missing markets show `等待数据`; stale/error states use amber/red semantics. Add a 44px tape and shared inspector block.

- [ ] **Step 4: Verify GREEN**

Run: `npm test -- --run src/lib/marketTapeViewModel.test.ts src/App.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add front/src/lib/marketTapeViewModel* front/src/components/terminal/MarketTape.tsx front/src/components/terminal/EvidenceHealth.tsx front/src/App.tsx front/src/pages front/src/App.css front/src/App.test.tsx
git commit -m "feat(front): add market and evidence tape"
```

### Task 3: Process event ledger

**Files:**
- Create: `src/lib/processEventViewModel.ts`
- Create: `src/lib/processEventViewModel.test.ts`
- Create: `src/components/terminal/ProcessEventLedger.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/components/terminal/TerminalComponents.test.tsx`
- Modify: `src/App.css`

**Interfaces:**
- Produces: `createProcessEventRows(events: FunnelEvent[]): ProcessEventRow[]`
- Rows expose `symbol`, `market`, `stage`, `result`, `source`, `latency`, `reason`, `timestamp`.

- [ ] **Step 1: Write failing ordering/source tests**

```ts
expect(createProcessEventRows([later, earlier]).map(row => row.id)).toEqual([later.id, earlier.id])
expect(rows[0]).toMatchObject({ source: '机会事件', latency: '4分钟' })
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/lib/processEventViewModel.test.ts src/components/terminal/TerminalComponents.test.tsx`
Expected: FAIL because the event ledger does not exist.

- [ ] **Step 3: Implement event rows and ledger**

Order by timestamp descending, then sequence descending. Translate source codes, preserve reasons, and render below Process Book without fabricating absent times.

- [ ] **Step 4: Verify GREEN and commit**

Run: `npm test -- --run src/lib/processEventViewModel.test.ts src/components/terminal/TerminalComponents.test.tsx`
Expected: PASS.

```bash
git add front/src/lib/processEventViewModel* front/src/components/terminal front/src/pages/ThemePage.tsx front/src/App.css
git commit -m "feat(front): add process event ledger"
```

### Task 4: Additive holding evidence and richer portfolio/risk/review content

**Files:**
- Modify: `src/types/dashboard.ts`
- Modify: `src/server/tradingAgentSnapshot.ts`
- Modify: `src/server/tradingAgentSnapshot.test.ts`
- Modify: `src/lib/terminalViewModels.ts`
- Modify: `src/lib/terminalViewModels.test.ts`
- Modify: `src/components/terminal/PortfolioLedger.tsx`
- Modify: `src/components/terminal/RiskLedger.tsx`
- Modify: `src/components/tables/SignalTable.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/App.css`

**Interfaces:**
- Adds optional `HoldingRow` fields: `quantity`, `averagePrice`, `markPrice`, `costBasis`, `marketValue`, `dayPnl`, `currency`, `updatedAt`, `source`.
- Extends `PortfolioLedgerRow` with formatted sourced evidence.

- [ ] **Step 1: Write failing server and view-model tests**

```ts
expect(snapshot.holdings[0]).toMatchObject({ quantity: 100, averagePrice: 12.4, markPrice: 12.8, source: expect.any(String) })
expect(createPortfolioLedgerRows(holdings)[0]).toMatchObject({ quantity: '100', averagePrice: '¥12.40', markPrice: '¥12.80' })
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/server/tradingAgentSnapshot.test.ts src/lib/terminalViewModels.test.ts`
Expected: FAIL because optional evidence is not carried through.

- [ ] **Step 3: Implement additive parsing and content**

Populate only values present in position snapshots/ledgers. Extend holdings columns; add market/currency exposure and stale-domain rows to risk; show confidence, actual impact, evidence and automatic calibration in review.

- [ ] **Step 4: Verify GREEN and commit**

Run: `npm test -- --run src/server/tradingAgentSnapshot.test.ts src/lib/terminalViewModels.test.ts src/components/terminal/TerminalComponents.test.tsx src/App.test.tsx`
Expected: PASS.

```bash
git add front/src/types front/src/server front/src/lib/terminalViewModels* front/src/components front/src/pages/ThemePage.tsx front/src/App.css
git commit -m "feat(front): expose sourced portfolio evidence"
```

### Task 5: Search, sort, column visibility, URL state and shortcuts

**Files:**
- Create: `src/lib/terminalTableState.ts`
- Create: `src/lib/terminalTableState.test.ts`
- Create: `src/components/terminal/TerminalTableToolbar.tsx`
- Create: `src/hooks/useTerminalNavigation.ts`
- Create: `src/hooks/useTerminalNavigation.test.tsx`
- Modify: `src/components/terminal/ProcessBook.tsx`
- Modify: `src/components/terminal/ProcessEventLedger.tsx`
- Modify: `src/components/terminal/PortfolioLedger.tsx`
- Modify: `src/components/terminal/RiskLedger.tsx`
- Modify: `src/components/TopNav.tsx`
- Modify: `src/components/charts/PerformanceChart.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/App.tsx`
- Modify: `src/App.css`

**Interfaces:**
- Produces: `filterAndSortRows(rows, query, accessor, direction)`.
- Produces hook: `useTerminalNavigation({ page, market, range, setPage, setMarket, setRange })`.
- URL keys: `page`, `market`, `range`.

- [ ] **Step 1: Write failing table and navigation tests**

```ts
expect(filterAndSortRows(rows, 'btc', row => row.symbol, 'asc')[0].symbol).toBe('BTC-USD')
expect(new URL(window.location.href).searchParams.get('page')).toBe('风险')
fireEvent.keyDown(window, { key: '2', altKey: true })
expect(setPage).toHaveBeenCalledWith('收益')
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/lib/terminalTableState.test.ts src/hooks/useTerminalNavigation.test.tsx src/App.test.tsx`
Expected: FAIL because controls and persisted navigation do not exist.

- [ ] **Step 3: Implement controls and navigation**

Use native inputs/details/checkboxes. Ignore shortcuts in editable elements. Make PerformanceChart range optionally controlled and store the returns range in the URL.

- [ ] **Step 4: Verify GREEN and commit**

Run: `npm test -- --run src/lib/terminalTableState.test.ts src/hooks/useTerminalNavigation.test.tsx src/components/terminal/TerminalComponents.test.tsx src/App.test.tsx`
Expected: PASS.

```bash
git add front/src/lib/terminalTableState* front/src/hooks front/src/components front/src/pages/ThemePage.tsx front/src/App.tsx front/src/App.css
git commit -m "feat(front): add terminal navigation controls"
```

### Task 6: Documentation, full regression, design QA and release

**Files:**
- Modify: `DESIGN.md`
- Modify: `README.md`
- Modify: `design-qa.md`
- Modify: `../STATUS.md`

- [ ] **Step 1: Update product/data documentation**

Document resolver priority, market tape, optional holding fields, event ledger, keyboard shortcuts, URL state and unchanged read-only boundary.

- [ ] **Step 2: Run full automated verification**

```bash
npm run lint
npm test -- --reporter=dot
npm run build:all
```

Expected: all commands exit 0.

- [ ] **Step 3: Run desktop browser QA**

At 1440×900 and 1280×720 verify all six pages, URL reload/back-forward, market tape, shortcuts, table controls, useful idle fallback, no horizontal document overflow and no console errors. Compare the 1440×900 returns view with the saved Hyperliquid reference in one visual input.

- [ ] **Step 4: Score and record design QA**

Record the eight-part Design Taste scorecard in `design-qa.md`; continue iteration until total is at least 85/100 and mark `final result: passed` only then.

- [ ] **Step 5: Commit docs**

```bash
git add front/DESIGN.md front/README.md front/design-qa.md STATUS.md
git commit -m "docs: record terminal operations validation"
```

- [ ] **Step 6: Safe release**

Fast-forward GitHub `main`, back up production `front/dist` and `front/dist-server`, fast-forward production source, build with Node v24.4.1, restart `tradingagent-front-api.service`, then separately verify production commit, service health, public HTML assets and public snapshot domains.
