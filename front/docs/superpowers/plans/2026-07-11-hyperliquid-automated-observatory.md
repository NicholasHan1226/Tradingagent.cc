# Hyperliquid Automated Observatory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the TradingAgent desktop frontend into a Hyperliquid-structured, read-only observatory that shows automated process and result state without asking Nicholas to make trading decisions.

**Architecture:** Preserve `WorkbenchViewModel` as selected-market/account truth, then compose it through a new `AutomationObservatoryViewModel` that owns process classification, runtime-rail priority, counts, and display language. React components render the derived model through a shared Hyperliquid-like shell; the snapshot API and backend automation contract remain read-only and unchanged.

**Tech Stack:** React 19, TypeScript 6, Vite 8, Recharts 3, Vitest, Testing Library, oxlint, Browser plugin runtime, Node snapshot API.

## Global Constraints

- The frontend remains strictly read-only: no queue writes, execution controls, callbacks, account mutations, or real-money enablement.
- Primary navigation is exactly `总览 / 收益 / 过程 / 持仓 / 风险 / 复盘`.
- `机会` and `决策` are legacy destinations normalized to `过程`; they are not visible navigation pages.
- User-visible content describes automated results and process only; remove language that asks Nicholas to decide, approve, follow up, or take a next step.
- The active live-account state remains globally gated on every navigation destination.
- Demo data remains local-preview-only; production snapshot failure never falls back to sample money, positions, or signals.
- Accepted desktop viewports are 1280x720 and 1440x900. Mobile information architecture remains deferred.
- At 1280x720, the blotter tabs, header, and at least one row must be visible without horizontal body overflow.
- At 1440x900, at least two blotter rows must be visible when data exists.
- Design QA must remain at or above 85/100 before production sync; target 90/100.
- Local `main`, GitHub `main`, production files, production runtime, and the public route are verified separately.

---

## File Structure

### New files

- `front/src/lib/automationObservatoryViewModel.ts`: derive the automation summary, canonical runtime item, running/completed/review rows, and process-language labels from `WorkbenchViewModel`.
- `front/src/lib/automationObservatoryViewModel.test.ts`: lock classification, runtime priority, idle behavior, and forbidden user-decision language.
- `front/src/components/workbench/RuntimeRail.tsx`: render the canonical current automation state without a primary action.
- `front/src/components/workbench/RuntimeRail.test.tsx`: verify running, blocked, completed, and idle states plus the absence of decision controls.
- `front/src/components/tables/RunningProcessTable.tsx`: accessible running-process table with automation-specific columns.

### Modified files

- `front/src/types/dashboard.ts`: replace the visible seven-page union with the six approved pages and add a `LegacyPage` normalization input.
- `front/src/data/dashboard.ts`: update `pages`, `pageMeta`, and copy to result/process language.
- `front/src/lib/chartEvents.ts`: route legacy opportunity and decision events to `过程`.
- `front/src/App.tsx`: create and pass `AutomationObservatoryViewModel`; use `总览` as the default page.
- `front/src/App.test.tsx`: verify six-page navigation, global live gate, process routing, and no decision language.
- `front/src/components/TopNav.tsx`: render the 56px six-page shell and automation state.
- `front/src/components/MarketHeader.tsx`: show running/completed counts and remove report-style recommendation copy.
- `front/src/components/MarketHeader.test.tsx`: verify result/process metrics and snapshot truth.
- `front/src/components/charts/PerformanceChart.tsx`: keep the accessible chart while supporting the compact integrated result header.
- `front/src/components/panels/RealtimeReturnCard.tsx`: reduce to a compact chart-header result summary or retire from the homepage.
- `front/src/components/panels/SignalFunnelFlow.tsx`: rename stages and language to the approved automated pipeline.
- `front/src/components/workbench/WorkbenchShell.tsx`: replace `ReviewRail` with `RuntimeRail` and retain one continuous workbench.
- `front/src/components/workbench/WorkbenchBlotter.tsx`: rename tabs and render the running-process table.
- `front/src/components/workbench/WorkbenchBlotter.test.tsx`: verify running/completed/review contracts.
- `front/src/components/tables/OpportunityTable.tsx`: stop serving as the homepage running table; remain only if a secondary compatibility path still imports it.
- `front/src/components/tables/SignalTable.tsx`: use automatic-result/review language and evidence fields already available.
- `front/src/pages/HomeDashboard.tsx`: consume the observatory model and remove user-action framing.
- `front/src/pages/ThemePage.tsx`: merge opportunity and decision content into `过程` and remove duplicate metrics.
- `front/src/components/PageSummaryBoard.tsx`: update page-specific summaries to result/process language.
- `front/src/App.css`: implement the Hyperliquid-like shell geometry and runtime/blotter styling.
- `front/src/index.css`: adjust global surface, border, text, and state tokens.
- `front/src/styles/home-funnel.css`: style the five-stage automated pipeline at 64-76px desktop height.
- `front/README.md`: document navigation, result/process language, and read-only automation boundary.
- `front/DESIGN.md`: record the final design direction, token decisions, component changes, scorecard, and next iteration.
- `STATUS.md`: record the frontend refactor and separately state local, GitHub, and production verification.

---

### Task 1: Reconcile the Feature Branch with Current Main

**Files:**
- Modify on conflict only: `STATUS.md`

**Interfaces:**
- Consumes: local `main` at `8120a80` or its verified fast-forward successor; feature commits through `96b89e9`.
- Produces: a feature branch containing both the July 11 production-validation status and all observatory design history.

- [ ] **Step 1: Fetch and inspect divergence without changing files**

Run:

```bash
git fetch origin
git status --short --branch
git log --oneline --left-right --cherry-pick main...HEAD
git diff --name-only main...HEAD
```

Expected: clean feature worktree; `main` contains the newer production-validation documentation; feature branch contains the workbench commits and approved observatory spec.

- [ ] **Step 2: Merge current main into the feature branch**

Run:

```bash
git merge --no-ff main -m "merge: reconcile observatory work with main"
```

Expected: either a clean merge or a conflict limited to `STATUS.md`.

- [ ] **Step 3: Resolve `STATUS.md` by preserving both truths if required**

The resolved top of `STATUS.md` must keep the latest production-validation entry and add no claim that the new frontend is deployed. The frontend entry is added only after Task 6 validation.

Run:

```bash
rg -n "2026-07-11 生产验收|Hyperliquid|<<<<<<<|=======|>>>>>>>" STATUS.md
git diff --check
```

Expected: production validation remains present; no conflict markers; no deployment claim.

- [ ] **Step 4: Commit only if conflict resolution created staged content**

```bash
git add STATUS.md
git commit --no-edit
```

Expected: merge commit completes without rewriting either branch history.

---

### Task 2: Add the Canonical Automation Observatory Model

**Files:**
- Create: `front/src/lib/automationObservatoryViewModel.ts`
- Create: `front/src/lib/automationObservatoryViewModel.test.ts`
- Modify: `front/src/types/dashboard.ts`
- Modify: `front/src/data/dashboard.ts`
- Modify: `front/src/lib/chartEvents.ts`
- Test: `front/src/lib/automationObservatoryViewModel.test.ts`
- Test: `front/src/lib/chartEvents.test.ts`

**Interfaces:**
- Consumes: `WorkbenchViewModel`, `SignalRow`, `FunnelEvent`, and `formatRuntimeReason`.
- Produces:

```ts
export type AutomationRuntimeKind = 'running' | 'waiting' | 'blocked' | 'completed' | 'idle'

export type AutomationRuntimeItem = {
  kind: AutomationRuntimeKind
  symbol: string | null
  name: string
  market: Market | null
  strategy: string
  stage: string
  statusLabel: string
  evidenceLabel: string
  updatedAtLabel: string
  detail: string
}

export type AutomationObservatoryViewModel = {
  running: SignalRow[]
  positions: HoldingRow[]
  completed: SignalRow[]
  automaticReview: SignalRow[]
  runtimeItem: AutomationRuntimeItem
  summary: {
    runningCount: number
    positionCount: number
    completedCount: number
    automaticReviewCount: number
  }
}

export function createAutomationObservatoryViewModel(
  workbench: WorkbenchViewModel,
): AutomationObservatoryViewModel

export function normalizePage(page: Page | LegacyPage): Page
```

- [ ] **Step 1: Write failing classification and runtime-priority tests**

Add tests equivalent to:

```ts
it('prioritizes running automation over terminal review rows', () => {
  const model = createAutomationObservatoryViewModel(workbench({
    active: [pendingSignal],
    completed: [executedSignal],
    review: [blockedSignal],
  }))

  expect(model.runtimeItem.kind).toBe('running')
  expect(model.running).toEqual([pendingSignal])
  expect(model.completed).toContain(executedSignal)
  expect(model.automaticReview).toContain(blockedSignal)
})

it('never returns user-decision language', () => {
  const model = createAutomationObservatoryViewModel(workbench({ active: [] }))
  expect(JSON.stringify(model)).not.toMatch(/下一步|还差什么|待处理|需要复盘/)
})

it.each([
  ['机会', '过程'],
  ['决策', '过程'],
  ['主页', '总览'],
] as const)('normalizes legacy page %s to %s', (legacy, expected) => {
  expect(normalizePage(legacy)).toBe(expected)
})
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:

```bash
npm test -- --run src/lib/automationObservatoryViewModel.test.ts src/lib/chartEvents.test.ts
```

Expected: FAIL because the new model and six-page union do not exist.

- [ ] **Step 3: Implement the model with one classification source**

Use `workbench.opportunities.active` for `running`,
`workbench.opportunities.completed` for terminal results, and
`workbench.reviewItems` for automatic review. Do not re-read raw unfiltered
signals. Select `runtimeItem` in this order: running row, blocked/waiting review
row, latest completed row, idle.

The idle item must be exactly:

```ts
{
  kind: 'idle',
  symbol: null,
  name: '当前没有运行中的自动任务',
  market: null,
  strategy: '自动化系统',
  stage: '运行空闲',
  statusLabel: '等待下一轮调度',
  evidenceLabel: '快照正常',
  updatedAtLabel: '等待新事件',
  detail: '收益、持仓和历史结果继续保留。',
}
```

Update the page types:

```ts
export type Page = '总览' | '收益' | '过程' | '持仓' | '风险' | '复盘'
export type LegacyPage = '主页' | '机会' | '决策'
```

Update chart events so positive and opportunity events target `过程`, while
risk events continue to target `风险`.

- [ ] **Step 4: Run model, chart-event, and dashboard tests**

```bash
npm test -- --run src/lib/automationObservatoryViewModel.test.ts src/lib/chartEvents.test.ts src/lib/workbenchViewModel.test.ts src/lib/dashboard.test.ts
```

Expected: all selected tests PASS; partial rows remain terminal.

- [ ] **Step 5: Commit the canonical model**

```bash
git add front/src/lib/automationObservatoryViewModel.ts front/src/lib/automationObservatoryViewModel.test.ts front/src/types/dashboard.ts front/src/data/dashboard.ts front/src/lib/chartEvents.ts front/src/lib/chartEvents.test.ts
git commit -m "refactor: model automated process and results"
```

---

### Task 3: Rebuild Navigation and Market Strip as a Hyperliquid Shell

**Files:**
- Modify: `front/src/App.tsx`
- Modify: `front/src/App.test.tsx`
- Modify: `front/src/components/TopNav.tsx`
- Modify: `front/src/components/MarketHeader.tsx`
- Modify: `front/src/components/MarketHeader.test.tsx`
- Modify: `front/src/App.css`
- Modify: `front/src/index.css`

**Interfaces:**
- Consumes: `AutomationObservatoryViewModel.summary`, `Page`, `Market`, and existing portfolio headline fields.
- Produces: six-page navigation, 56px global shell, compact market strip, and App-level observatory routing.

- [ ] **Step 1: Write failing navigation and market-strip tests**

Add App assertions:

```ts
expect(within(screen.getByRole('navigation', { name: '主导航' })).getAllByRole('button'))
  .toHaveLength(6)
expect(screen.getByRole('button', { name: '总览' })).toBeInTheDocument()
expect(screen.getByRole('button', { name: '过程' })).toBeInTheDocument()
expect(screen.queryByRole('button', { name: '机会' })).not.toBeInTheDocument()
expect(screen.queryByRole('button', { name: '决策' })).not.toBeInTheDocument()

const marketHeader = screen.getByRole('region', { name: '市场与账户' })
expect(within(marketHeader).getByText('运行中').parentElement).toHaveTextContent('2')
expect(within(marketHeader).getByText('已完成').parentElement).toHaveTextContent('4')
```

- [ ] **Step 2: Run tests and confirm the old shell fails**

```bash
npm test -- --run src/App.test.tsx src/components/MarketHeader.test.tsx
```

Expected: FAIL on old labels, seven-page navigation, and old `市场概览` region name.

- [ ] **Step 3: Wire App to the observatory model**

Create the model once:

```ts
const observatory = useMemo(
  () => createAutomationObservatoryViewModel(workbench),
  [workbench],
)
```

Default `activePage` to `总览`. Pass observatory counts to `MarketHeader`, and
route `总览` to `HomeDashboard`. Keep `workbench.liveGate.gated` in the routing
condition so every page remains gated in live mode.

- [ ] **Step 4: Implement the shared shell geometry and tokens**

Set the desktop rules:

```css
:root {
  --bg-app: #0b1118;
  --surface-base: #101821;
  --surface-raised: #141e28;
  --border-hairline: rgba(195, 220, 226, 0.10);
  --text-primary: #f1f5f5;
  --text-secondary: #a9b5bc;
  --text-muted: #71808a;
  --accent-cyan: #71d7c7;
  --state-amber: #d6ad63;
  --state-red: #e46f78;
}

.top-nav { height: 56px; }
.market-header { min-height: 108px; }
.workbench-primary-grid { grid-template-columns: minmax(0, 1fr) 320px; }
```

Keep 4/8/12/16/24/32px spacing, 3-4px panel radii, no card shadows, no glow,
and visible `:focus-visible` outlines.

- [ ] **Step 5: Run navigation tests and lint**

```bash
npm run lint
npm test -- --run src/App.test.tsx src/components/MarketHeader.test.tsx
```

Expected: PASS; no visible opportunity/decision navigation.

- [ ] **Step 6: Commit the shell**

```bash
git add front/src/App.tsx front/src/App.test.tsx front/src/components/TopNav.tsx front/src/components/MarketHeader.tsx front/src/components/MarketHeader.test.tsx front/src/App.css front/src/index.css
git commit -m "style: align automated observatory shell with hyperliquid"
```

---

### Task 4: Replace the Review Rail with a Current Runtime Rail

**Files:**
- Create: `front/src/components/workbench/RuntimeRail.tsx`
- Create: `front/src/components/workbench/RuntimeRail.test.tsx`
- Modify: `front/src/components/workbench/WorkbenchShell.tsx`
- Modify: `front/src/pages/HomeDashboard.tsx`
- Modify: `front/src/components/panels/RealtimeReturnCard.tsx`
- Modify: `front/src/components/charts/PerformanceChart.tsx`
- Modify: `front/src/App.css`

**Interfaces:**
- Consumes: `AutomationRuntimeItem`, observatory counts, canonical portfolio result, and existing chart data.
- Produces: `RuntimeRail({ item, runningCount })` and one integrated result-chart header with no duplicate primary return card.

- [ ] **Step 1: Write failing runtime-rail tests**

```tsx
render(<RuntimeRail item={runningItem} runningCount={2} />)

expect(screen.getByRole('complementary', { name: '当前运行' })).toHaveTextContent('模拟执行')
expect(screen.getByText('运行中 2')).toBeInTheDocument()
expect(screen.queryByRole('button')).not.toBeInTheDocument()
expect(screen.queryByText(/下一步|还差什么|查看完整记录|需要复盘/)).not.toBeInTheDocument()
```

Add idle, safety-block, and completed-result cases using explicit fixture items.

- [ ] **Step 2: Run tests and confirm the component is absent**

```bash
npm test -- --run src/components/workbench/RuntimeRail.test.tsx
```

Expected: FAIL because `RuntimeRail` does not exist.

- [ ] **Step 3: Implement the runtime rail**

Render these fixed fields:

```tsx
<aside aria-label="当前运行" className={`runtime-rail ${item.kind}`}>
  <header>
    <span>自动化状态</span>
    <b>运行中 {runningCount}</b>
  </header>
  <section>
    <small>{item.market ? marketLabels[item.market] : '全市场'} · {item.symbol ?? 'AUTO'}</small>
    <h2>{item.name}</h2>
    <dl>
      <div><dt>过程</dt><dd>{item.strategy}</dd></div>
      <div><dt>阶段</dt><dd>{item.stage}</dd></div>
      <div><dt>状态</dt><dd>{item.statusLabel}</dd></div>
      <div><dt>证据</dt><dd>{item.evidenceLabel}</dd></div>
      <div><dt>更新时间</dt><dd>{item.updatedAtLabel}</dd></div>
    </dl>
    <p>{item.detail}</p>
  </section>
</aside>
```

Do not render a button, link styled as a primary action, recommendation, or
order-shaped input.

- [ ] **Step 4: Merge the return card into the chart header**

The chart header contains one primary return, account-mode tabs, target gap,
risk distance, and snapshot state. Remove the separate homepage headline card
wrapper. Preserve the chart `aria-describedby` summary and keep event buttons
outside the image role.

- [ ] **Step 5: Run runtime, chart, and App tests**

```bash
npm test -- --run src/components/workbench/RuntimeRail.test.tsx src/components/workbench/ChartAccessibleSummary.test.tsx src/App.test.tsx
npm run lint
```

Expected: PASS; no manual-action language or duplicate primary return region.

- [ ] **Step 6: Commit the primary workspace**

```bash
git add front/src/components/workbench/RuntimeRail.tsx front/src/components/workbench/RuntimeRail.test.tsx front/src/components/workbench/WorkbenchShell.tsx front/src/pages/HomeDashboard.tsx front/src/components/panels/RealtimeReturnCard.tsx front/src/components/charts/PerformanceChart.tsx front/src/App.css
git commit -m "refactor: show current automated runtime state"
```

---

### Task 5: Convert the Funnel and Blotter to Automated Process and Results

**Files:**
- Create: `front/src/components/tables/RunningProcessTable.tsx`
- Modify: `front/src/components/panels/SignalFunnelFlow.tsx`
- Modify: `front/src/components/workbench/WorkbenchBlotter.tsx`
- Modify: `front/src/components/workbench/WorkbenchBlotter.test.tsx`
- Modify: `front/src/components/tables/SignalTable.tsx`
- Modify: `front/src/components/tables/HoldingsTable.tsx`
- Modify: `front/src/styles/home-funnel.css`
- Modify: `front/src/App.css`

**Interfaces:**
- Consumes: `observatory.running`, `observatory.positions`, `observatory.completed`, `observatory.automaticReview`, and real `FunnelEvent[]`.
- Produces: four result/process tabs and a five-stage automated pipeline.

- [ ] **Step 1: Write failing tab and process-table tests**

```tsx
expect(screen.getByRole('tab', { name: '运行中 1' })).toBeInTheDocument()
expect(screen.getByRole('tab', { name: '自动复盘 1' })).toBeInTheDocument()
expect(screen.queryByRole('tab', { name: /当前机会|待复盘/ })).not.toBeInTheDocument()

const runningPanel = screen.getByRole('tabpanel', { name: '运行中' })
expect(within(runningPanel).getByRole('table', { name: '自动运行过程表' })).toBeInTheDocument()
expect(within(runningPanel).getByRole('columnheader', { name: '当前阶段' })).toBeInTheDocument()
expect(within(runningPanel).queryByText(/下一步|还差什么/)).not.toBeInTheDocument()
```

- [ ] **Step 2: Run tests and confirm old labels fail**

```bash
npm test -- --run src/components/workbench/WorkbenchBlotter.test.tsx
```

Expected: FAIL on old `当前机会` and `待复盘` labels.

- [ ] **Step 3: Implement `RunningProcessTable`**

Render an accessible table with columns:

```ts
['自动过程', '市场', '当前阶段', '运行状态', '证据', '更新时间']
```

Map existing fields conservatively:

- process: `strategyName ?? method`;
- current stage: `stage ?? '自动等待'`;
- state: translated `reason`;
- evidence: `stageEvidence === 'full' ? '证据完整' : stageEvidence === 'replay' ? '历史回放' : '证据有限'`;
- time: `age`.

- [ ] **Step 4: Rename and tighten the pipeline**

Use the exact stage labels:

```ts
const AUTOMATION_STAGES = ['发现', '研究', '风控', '模拟执行', '结果写回'] as const
```

Replace conversion copy with throughput and bottleneck copy. In the empty state,
show `当前没有运行中的自动过程`; when only holdings exist, show
`运行空闲 · 持仓继续盯市`.

- [ ] **Step 5: Run blotter and funnel tests**

```bash
npm test -- --run src/components/workbench/WorkbenchBlotter.test.tsx src/App.test.tsx
npm run lint
```

Expected: PASS; partial remains completed; no manual-decision language.

- [ ] **Step 6: Commit the process surfaces**

```bash
git add front/src/components/tables/RunningProcessTable.tsx front/src/components/panels/SignalFunnelFlow.tsx front/src/components/workbench/WorkbenchBlotter.tsx front/src/components/workbench/WorkbenchBlotter.test.tsx front/src/components/tables/SignalTable.tsx front/src/components/tables/HoldingsTable.tsx front/src/styles/home-funnel.css front/src/App.css
git commit -m "feat: present automated process and result blotter"
```

---

### Task 6: Merge Secondary Pages and Remove Decision-Oriented Copy

**Files:**
- Modify: `front/src/pages/ThemePage.tsx`
- Modify: `front/src/components/PageSummaryBoard.tsx`
- Modify: `front/src/components/panels/DecisionFormation.tsx`
- Modify: `front/src/components/panels/OpportunityFocus.tsx`
- Modify: `front/src/components/panels/HomeResultBrief.tsx`
- Modify: `front/src/components/panels/ResultSummary.tsx`
- Modify: `front/src/components/panels/RiskSnapshot.tsx`
- Modify: `front/src/components/panels/SignalDepth.tsx`
- Modify: `front/src/App.test.tsx`
- Modify: `front/src/App.css`

**Interfaces:**
- Consumes: six-page `Page`, observatory collections, existing result/risk panels, and domain-status boundaries.
- Produces: one `过程` page and result/process-only copy across all pages.

- [ ] **Step 1: Write failing process-page and forbidden-copy tests**

```ts
fireEvent.click(screen.getByRole('button', { name: '过程' }))
expect(screen.getByRole('heading', { name: '自动化过程' })).toBeInTheDocument()
expect(screen.getByText('运行阶段')).toBeInTheDocument()

const userVisibleText = document.body.textContent ?? ''
expect(userVisibleText).not.toMatch(/你应该|建议操作|下一步|还差什么|需要复盘|当前机会/)
```

Add navigation loops for all six pages and assert each page renders one primary
summary region and no framework error boundary.

- [ ] **Step 2: Run App tests and confirm old page behavior fails**

```bash
npm test -- --run src/App.test.tsx
```

Expected: FAIL because old opportunity and decision pages still exist and copy remains decision-oriented.

- [ ] **Step 3: Implement the merged process page**

The `过程` page contains:

- stage distribution and throughput;
- active automation table;
- safety-block and strategy-wait summaries;
- completed path timeline;
- evidence completeness and processing latency when fields exist.

It must not contain an opportunity CTA or a recommendation. Reuse existing
panels only after renaming their public content; do not duplicate the homepage
runtime rail.

- [ ] **Step 4: Apply page-specific content contracts**

- `收益`: result chart plus market/strategy contribution.
- `持仓`: position table, allocation, and concentration/risk.
- `风险`: drawdown, limit distance, safety blocks, upstream data gate, timeline.
- `复盘`: completed/partial/missed/cancelled/blocked outcomes and automatic attribution.

Keep A-share research, moneyflow, forward validation, and tier evidence below
the primary blotter and only for `All Markets` or `A-share`.

- [ ] **Step 5: Run all frontend tests and builds**

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
git diff --check
```

Expected: all commands PASS; no TypeScript route mismatch remains.

- [ ] **Step 6: Commit secondary-page cleanup**

```bash
git add front/src/pages/ThemePage.tsx front/src/components/PageSummaryBoard.tsx front/src/components/panels front/src/App.test.tsx front/src/App.css
git commit -m "refactor: focus pages on automated process and results"
```

---

### Task 7: Rendered QA, Documentation, and Design Gate

**Files:**
- Modify: `front/README.md`
- Modify: `front/DESIGN.md`
- Modify: `STATUS.md`
- Verify only: all changed frontend source and tests.
- Temporary screenshots: `/tmp/tradingagent-automated-observatory-20260711/`

**Interfaces:**
- Consumes: completed local implementation and the accepted Hyperliquid reference screenshot.
- Produces: browser evidence, updated design score, clean feature branch, and a release candidate commit.

- [ ] **Step 1: Start the local preview with explicit demo mode**

```bash
VITE_TRADING_AGENT_DEMO_PREVIEW=1 npm run dev -- --host 127.0.0.1
```

Expected: Vite serves the app on the reported localhost port without a framework error.

- [ ] **Step 2: Validate the target flow with the Browser plugin**

The flow under test is: `总览` loads -> six-page navigation and market switching
work -> automated runtime/process/result surfaces remain read-only -> live mode
gates every navigation destination.

Required checks:

- URL and title identify TradingAgent;
- DOM snapshot contains `市场与账户`, `当前运行`, `自动化过程`, and `运行中`;
- no framework overlay;
- no relevant console error or warning;
- navigation to all six pages renders meaningful content;
- market switch changes the selected-market content;
- live mode plus navigation still shows `实盘待接入`;
- no button or input matches `买|卖|下单|撤单|批准|确认交易`;
- no visible text matches `下一步|还差什么|需要复盘|当前机会`.

- [ ] **Step 3: Validate geometry at both desktop viewports**

At 1280x720, evaluate:

```js
({
  bodyScrollWidth: document.body.scrollWidth,
  viewportWidth: innerWidth,
  firstRowBottom: document.querySelector('[role="tabpanel"] [role="row"]:nth-child(2)')?.getBoundingClientRect().bottom,
  viewportHeight: innerHeight,
})
```

Expected: body width equals viewport width and `firstRowBottom <= viewportHeight`.

Repeat at 1440x900 and verify two data rows are visible when fixtures provide them.

- [ ] **Step 4: Capture the final screenshot set outside the repository**

Write:

```text
/tmp/tradingagent-automated-observatory-20260711/final-1280x720.png
/tmp/tradingagent-automated-observatory-20260711/final-1440x900.png
/tmp/tradingagent-automated-observatory-20260711/live-gate-1280x720.png
```

Compare against:

```text
/tmp/tradingagent-hyperliquid-audit-20260710/02-hyperliquid-trade-desktop.png
```

The mismatch ledger must cover navigation density, market strip, chart/right
rail ratio, pipeline/blotter geometry, and intentional read-only deviation.

- [ ] **Step 5: Update documentation and scorecard**

`front/DESIGN.md` must include:

- Design Direction;
- Token Decisions;
- Component Changes;
- eight-category Scorecard;
- exactly three Next Iteration items.

`STATUS.md` must say the frontend is locally verified only until Tasks 8 and 9
prove GitHub and production state.

- [ ] **Step 6: Run final verification after documentation changes**

```bash
npm run lint
npm test -- --run
TZ=America/Los_Angeles npm test -- --run src/lib/format.test.ts
npm run build
npm run build:api
git diff --check
```

Expected: all checks PASS with a clean build and the cross-timezone test green.

- [ ] **Step 7: Commit the release candidate**

```bash
git add front/README.md front/DESIGN.md STATUS.md front/src
git commit -m "docs: validate automated observatory release candidate"
git status --short --branch
```

Expected: clean feature worktree.

---

### Task 8: Merge to Local Main and Push GitHub Main

**Files:**
- Git refs only; no source edits expected.

**Interfaces:**
- Consumes: clean verified feature branch and clean current local `main`.
- Produces: local `main` and `origin/main` at the same verified commit.

- [ ] **Step 1: Run safe-release preflight**

```bash
git status --short --branch
git fetch origin
git rev-parse HEAD
git rev-parse main
git rev-parse origin/main
git log --oneline --left-right main...HEAD
```

Expected: clean feature branch; no unknown remote commit; rollback is the
pre-merge local-main SHA.

- [ ] **Step 2: Verify the main worktree is clean**

```bash
git -C /Users/nicholashan/Projects/Finance/TradingAgent status --short --branch
```

Expected: clean `main`. If dirty, stop and classify ownership instead of
overwriting files.

- [ ] **Step 3: Merge the feature branch into local main**

```bash
git -C /Users/nicholashan/Projects/Finance/TradingAgent merge --no-ff codex/hyperliquid-workbench -m "merge: ship automated observatory"
```

Expected: merge succeeds without history rewrite.

- [ ] **Step 4: Re-run release checks on local main**

```bash
cd /Users/nicholashan/Projects/Finance/TradingAgent/front
npm run lint
npm test -- --run
npm run build
npm run build:api
git -C .. diff --check origin/main..main
```

Expected: all checks PASS on local main.

- [ ] **Step 5: Push and prove GitHub main**

```bash
git -C /Users/nicholashan/Projects/Finance/TradingAgent push origin main
git -C /Users/nicholashan/Projects/Finance/TradingAgent fetch origin
test "$(git -C /Users/nicholashan/Projects/Finance/TradingAgent rev-parse main)" = "$(git -C /Users/nicholashan/Projects/Finance/TradingAgent rev-parse origin/main)"
```

Expected: push succeeds and local/remote SHAs match exactly.

---

### Task 9: Sync Production Files, Runtime, and Public Route

**Files:**
- Production source: `/opt/investment/tradingagent`
- Production static build: `/opt/investment/tradingagent/front/dist`
- Production API build: `/opt/investment/tradingagent/front/dist-server`
- Service: `tradingagent-front-api.service`

**Interfaces:**
- Consumes: verified GitHub `main` and the existing same-server Nginx/API deployment.
- Produces: production source SHA, static build, API runtime, and public route at the same release.

- [ ] **Step 1: Read-only production preflight**

```bash
ssh 8.138.181.177 'cd /opt/investment/tradingagent && git status --short --branch && git rev-parse HEAD && systemctl is-active tradingagent-front-api.service && curl -fsS http://127.0.0.1:8787/healthz'
```

Expected: production worktree ownership is understood, service is active, and
health returns `ok`. Any unknown dirty files block `git pull` until classified.

- [ ] **Step 2: Preserve rollback artifacts**

```bash
ssh 8.138.181.177 'set -e; cd /opt/investment/tradingagent/front; stamp=$(date +%Y%m%d%H%M%S); [ ! -d dist ] || cp -a dist "dist.rollback-$stamp"; [ ! -d dist-server ] || cp -a dist-server "dist-server.rollback-$stamp"; echo "$stamp" > /tmp/tradingagent-front-rollback-stamp'
```

Expected: previous static and API build directories have timestamped rollback copies.

- [ ] **Step 3: Fast-forward production source to GitHub main**

```bash
ssh 8.138.181.177 'set -e; cd /opt/investment/tradingagent; git fetch origin; git pull --ff-only origin main; git rev-parse HEAD; git rev-parse origin/main'
```

Expected: production HEAD equals origin/main with no merge commit created on the server.

- [ ] **Step 4: Build frontend and read-only API on production**

```bash
ssh 8.138.181.177 'set -e; cd /opt/investment/tradingagent/front; VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot /opt/investment/tools/node-v24.4.1/bin/npm run build; /opt/investment/tools/node-v24.4.1/bin/npm run build:api'
```

Expected: both builds succeed and new hashed frontend assets exist.

- [ ] **Step 5: Restart only the read-only frontend API service**

```bash
ssh 8.138.181.177 'set -e; sudo systemctl restart tradingagent-front-api.service; sudo systemctl is-active tradingagent-front-api.service; curl -fsS http://127.0.0.1:8787/healthz'
```

Expected: service is active and health returns `ok`. No trading service, cron,
queue consumer, or execution bridge is restarted.

- [ ] **Step 6: Verify production snapshot and public routes separately**

```bash
ssh 8.138.181.177 'curl -fsS http://127.0.0.1:8787/api/trading-agent/snapshot | /opt/investment/tools/node-v24.4.1/bin/node -e "let s=\"\";process.stdin.on(\"data\",d=>s+=d);process.stdin.on(\"end\",()=>{const x=JSON.parse(s);console.log(JSON.stringify({mode:x.mode,generatedAt:x.generatedAt,domains:x.domains},null,2))})"'
curl -fsSI https://dashboard.tradingagent.cc/
curl -fsS https://dashboard.tradingagent.cc/api/trading-agent/snapshot | head -c 200
```

Expected: internal snapshot is valid JSON; public dashboard returns success;
public snapshot returns JSON through Nginx/Cloudflare.

- [ ] **Step 7: Verify the public rendered page with Browser**

Navigate to `https://dashboard.tradingagent.cc/` and repeat page identity,
non-blank, framework overlay, console, screenshot, six-page navigation, live
gate, and no-order-control checks. Confirm the public page contains the new
`自动化状态` and `过程` labels.

- [ ] **Step 8: Record the three-surface proof**

Report separately:

- local main SHA and clean status;
- GitHub origin/main SHA;
- production source SHA;
- production API active state and health;
- public dashboard HTTP/render proof;
- public snapshot proof;
- rollback directory stamp.

If production build or runtime verification fails, restore the timestamped
`dist` and `dist-server` directories from Step 2, restart only
`tradingagent-front-api.service`, and verify health before reporting rollback.

---

### Task 10: Record Release Proof and Re-Align All Three Source Trees

**Files:**
- Modify: `STATUS.md`

**Interfaces:**
- Consumes: successful Task 9 production and public-route evidence.
- Produces: a final documentation commit present on local `main`, GitHub
  `origin/main`, and production source, while the already-verified frontend/API
  artifacts continue to serve the same application code.

- [ ] **Step 1: Write the verified release record**

Add one dated `STATUS.md` entry containing only measured facts:

- frontend release commit used for the production build;
- final documentation commit;
- production source path and source SHA;
- `tradingagent-front-api.service` active result;
- internal `/healthz` result;
- public dashboard and snapshot route results;
- accepted desktop viewports;
- test count and build results;
- rollback stamp;
- explicit statement that no execution, queue, capital, strategy, or real-money
  behavior changed.

- [ ] **Step 2: Commit and push the release record**

```bash
git add STATUS.md
git commit -m "docs: record automated observatory production sync"
git push origin main
git fetch origin
```

Expected: local `main` equals `origin/main` at the documentation commit.

- [ ] **Step 3: Fast-forward production source to the documentation commit**

```bash
ssh 8.138.181.177 'set -e; cd /opt/investment/tradingagent; git fetch origin; git pull --ff-only origin main; git rev-parse HEAD; git status --short --branch'
```

Expected: production source is clean and equals GitHub `main`. No rebuild or
service restart is necessary because the final commit modifies only
`STATUS.md`.

- [ ] **Step 4: Prove final three-surface equality and runtime continuity**

```bash
local_sha=$(git rev-parse main)
remote_sha=$(git rev-parse origin/main)
prod_sha=$(ssh 8.138.181.177 'cd /opt/investment/tradingagent && git rev-parse HEAD')
test "$local_sha" = "$remote_sha"
test "$local_sha" = "$prod_sha"
ssh 8.138.181.177 'systemctl is-active tradingagent-front-api.service && curl -fsS http://127.0.0.1:8787/healthz'
curl -fsSI https://dashboard.tradingagent.cc/
```

Expected: all three source SHAs match, the frontend API remains active and
healthy, and the public dashboard remains reachable.
