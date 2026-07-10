# Hyperliquid-Inspired Read-Only Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the TradingAgent desktop homepage as a continuous read-only trading workbench with canonical return data, truthful active-opportunity semantics, a gated live-account state, and Hyperliquid-inspired hierarchy.

**Architecture:** Add one pure `WorkbenchViewModel` boundary between snapshot data and UI composition. Recompose the homepage from focused workbench components while preserving the existing snapshot API, adapters, theme-page routes, and read-only execution boundary. Land truth fixes before visual composition so they remain independently revertible.

**Tech Stack:** React 19, TypeScript 6, Vite 8, Vitest 4, Testing Library, Recharts 3, existing CSS token system.

## Global Constraints

- Scope is `TradingAgent/front/` plus the required `TradingAgent/STATUS.md` handoff update.
- The frontend remains read-only and must not mutate signals, queues, accounts, callbacks, or execution state.
- No buy, sell, order, cancel, confirm, leverage, wallet, or broker controls.
- No mobile navigation, 390px layout, touch-target, or phone-specific responsive work in this phase.
- No new npm dependency unless the existing React, Recharts, and Lucide stack proves insufficient.
- No push, deployment, production service restart, or external write without separate Nicholas authorization.
- Every behavior change follows red-green-refactor; each task runs its targeted test before its commit.
- Final validation requires `npm run lint`, `npm test -- --run`, `npm run build`, and `npm run build:api`.

---

## File Map

**Create**

- `src/lib/workbenchViewModel.ts` — canonical selected-view portfolio, chart series, signal classification, headline, live gate, and runtime-copy derivation.
- `src/lib/workbenchViewModel.test.ts` — unit proof for headline/chart equality and active/completed opportunity semantics.
- `src/components/workbench/ChartAccessibleSummary.tsx` — screen-reader and concise visible chart summary.
- `src/components/workbench/ReviewRail.tsx` — selected-view next-review surface with no execution action.
- `src/components/workbench/WorkbenchBlotter.tsx` — tabbed active opportunities, positions, completed outcomes, and review items.
- `src/components/workbench/WorkbenchBlotter.test.tsx` — tab, empty-state, and terminal-row separation tests.
- `src/components/workbench/WorkbenchShell.tsx` — continuous homepage composition.

**Modify**

- `src/App.tsx` — derive and pass one `WorkbenchViewModel`.
- `src/App.test.tsx` — integration proof for conflicting raw inputs, workbench regions, live gate, and raw-copy removal.
- `src/lib/dashboard.ts` — remove active/closed fallback semantics and delegate shared classification.
- `src/lib/dashboard.test.ts` — lock strict empty active/closed behavior.
- `src/pages/HomeDashboard.tsx` — replace card stack with `WorkbenchShell`.
- `src/pages/ThemePage.tsx` — consume canonical performance and strict active/completed rows.
- `src/components/MarketHeader.tsx` — become the compact desktop market strip.
- `src/components/charts/PerformanceChart.tsx` — accessible label/description and canonical summary integration.
- `src/components/charts/ContributionPanel.tsx` — real empty state when attribution is unavailable.
- `src/components/tables/OpportunityTable.tsx` — remove historical fallback assumptions.
- `src/components/panels/ClosedLoopProofPanel.tsx` — centralized runtime copy.
- `src/App.css`, `src/styles/home-funnel.css`, `src/styles/page-summary.css`, `src/index.css` — continuous desktop workbench hierarchy.
- `README.md`, `DESIGN.md`, `../STATUS.md` — durable behavior, scorecard, verification, and handoff.

---

### Task 1: Canonical Workbench View Model

**Files:**

- Create: `src/lib/workbenchViewModel.ts`
- Create: `src/lib/workbenchViewModel.test.ts`
- Modify: `src/lib/dashboard.ts`
- Modify: `src/lib/dashboard.test.ts`

**Interfaces:**

- Consumes: `Market`, `AccountMode`, `PerformancePoint[]`, `PortfolioSummary | null`, `MarketSummary[]`, `SignalRow[]`, `HoldingRow[]`, `FunnelEvent[]`, `generatedAt`.
- Produces:

```ts
export type WorkbenchViewModel = {
  accountMode: AccountMode
  market: Market
  portfolio: PortfolioSummary | null
  performance: PerformancePoint[]
  headline: {
    pnlAmount: number | null
    returnPct: number
    targetPct: number
    targetGapPct: number
    maxDrawdownPct: number | null
    capitalBase: number | null
    generatedAt: string | null
  }
  opportunities: { active: SignalRow[]; completed: SignalRow[] }
  positions: HoldingRow[]
  funnelEvents: FunnelEvent[]
  reviewItems: SignalRow[]
  liveGate: { gated: boolean; title: string; detail: string }
}

export function createWorkbenchViewModel(input: {
  accountMode: AccountMode
  activeMarket: Market
  performance: PerformancePoint[]
  portfolio: PortfolioSummary | null
  marketSummaries: MarketSummary[]
  signals: SignalRow[]
  holdings: HoldingRow[]
  funnelEvents: FunnelEvent[]
  generatedAt: string | null
}): WorkbenchViewModel

export function formatRuntimeReason(reason?: string): string
```

- [ ] **Step 1: Write the failing canonical-data tests**

Create `src/lib/workbenchViewModel.test.ts` with these behaviors:

```ts
import { describe, expect, it } from 'vitest'
import { createWorkbenchViewModel, formatRuntimeReason } from './workbenchViewModel'
import type { MarketSummary, PortfolioSummary, SignalRow } from '../types/dashboard'

const portfolio: PortfolioSummary = {
  pnlAmount: -65,
  returnPct: -0.03,
  capitalBase: 200000,
  targetPct: 8,
  maxDrawdownPct: 0,
  tradeCount: 5,
  pointCount: 3,
  source: 'account',
  pnlCurrency: 'CNY',
  updatedAt: '2026-07-11T09:00:00+08:00',
}

const summaries: MarketSummary[] = [{
  market: 'A-share', status: 'ready', runtimeState: 'normal', holdingCount: 3,
  signalCount: 4, tradeCount: 3, styleCount: 1, capitalBase: 200000,
  pnlAmount: 6931, returnPct: 3.47, maxDrawdownPct: 0, source: 'market-summary',
  headline: 'A股', detail: 'A股结果', pnlCurrency: 'CNY',
}]

const signals: SignalRow[] = [
  { symbol: '000001.SZ', name: '平安银行', market: 'A-share', method: 'buy', status: 'pending', impact: '--', confidence: '70%', age: '1小时', reason: '等待确认', next: '继续观察', steps: 4 },
  { symbol: '000002.SZ', name: '万科A', market: 'A-share', method: 'buy', status: 'blocked', impact: '--', confidence: '60%', age: '2小时', reason: '风险偏高', next: '等待风险下降', steps: 4 },
  { symbol: '000003.SZ', name: '国农科技', market: 'A-share', method: 'buy', status: 'executed', impact: '--', confidence: '80%', age: '3小时', reason: '已成交', next: '进入复盘', steps: 6 },
  { symbol: '000004.SZ', name: '国华网安', market: 'A-share', method: 'buy', status: 'missed', impact: '--', confidence: '50%', age: '4小时', reason: '已错过', next: '进入复盘', steps: 5 },
]

describe('createWorkbenchViewModel', () => {
  it('forces the chart latest point to equal the selected headline return', () => {
    const view = createWorkbenchViewModel({
      accountMode: 'simulated', activeMarket: 'All Markets',
      performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
      portfolio, marketSummaries: summaries, signals, holdings: [], funnelEvents: [], generatedAt: '2026-07-11T09:00:00+08:00',
    })
    expect(view.performance.at(-1)?.simulated).toBe(view.headline.returnPct)
    expect(view.performance.at(-1)?.target).toBe(view.headline.targetPct)
  })

  it('separates active opportunities from terminal outcomes', () => {
    const view = createWorkbenchViewModel({ accountMode: 'simulated', activeMarket: 'All Markets', performance: [], portfolio, marketSummaries: [], signals, holdings: [], funnelEvents: [], generatedAt: null })
    expect(view.opportunities.active.map((row) => row.status)).toEqual(['pending', 'blocked'])
    expect(view.opportunities.completed.map((row) => row.status)).toEqual(['executed', 'missed'])
  })

  it('returns a dedicated live gate without changing the selected market', () => {
    const view = createWorkbenchViewModel({ accountMode: 'live', activeMarket: 'A-share', performance: [], portfolio, marketSummaries: summaries, signals, holdings: [], funnelEvents: [], generatedAt: null })
    expect(view.market).toBe('A-share')
    expect(view.liveGate).toEqual(expect.objectContaining({ gated: true, title: '实盘待接入' }))
  })
})

describe('formatRuntimeReason', () => {
  it.each([
    ['market_data_missing', '等待行情数据'],
    ['futures_market_data_not_ready', '期货行情尚未就绪'],
    ['crypto_waiting_for_market_data', '加密市场等待行情'],
  ])('maps %s to user copy', (input, expected) => {
    expect(formatRuntimeReason(input)).toBe(expected)
  })
})
```

Extend `src/lib/dashboard.test.ts`:

```ts
it('does not fall back to completed rows when there are no actionable signals', () => {
  expect(getActionableSignals(rows.filter((row) => row.status === 'executed'))).toEqual([])
})

it('does not fall back to active rows when there are no closed signals', () => {
  expect(getClosedSignals(rows.filter((row) => row.status === 'pending'))).toEqual([])
})
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
npm test -- --run src/lib/workbenchViewModel.test.ts src/lib/dashboard.test.ts
```

Expected: FAIL because `workbenchViewModel.ts` does not exist and the current dashboard helpers fall back to all rows.

- [ ] **Step 3: Implement the pure model and strict classifiers**

Implement `createWorkbenchViewModel()` using the existing `getPortfolioForView()`, `getVisibleSignals()`, and `getVisibleHoldings()` rules. Patch only the final performance row:

```ts
const performance = input.performance.length
  ? input.performance.map((point, index) => index === input.performance.length - 1
      ? { ...point, simulated: selectedPortfolio?.returnPct ?? point.simulated, target: selectedPortfolio?.targetPct ?? point.target }
      : point)
  : selectedPortfolio
    ? [{ day: '现在', simulated: selectedPortfolio.returnPct, target: selectedPortfolio.targetPct, benchmark: 0, opportunity: -Math.abs(selectedPortfolio.maxDrawdownPct) }]
    : []
```

Classify `pending` and `blocked` as active; classify `executed`, `missed`, and `cancelled` as completed. Return `blocked`, `missed`, and `cancelled` rows as review items. Keep `activeMarket` unchanged in live mode and return a dedicated gate object.

Change existing helpers to strict filters:

```ts
export function getActionableSignals(rows: SignalRow[]) {
  return rows.filter((signal) => signal.status === 'pending' || signal.status === 'blocked')
}

export function getClosedSignals(rows: SignalRow[]) {
  return rows.filter((signal) => signal.status === 'executed' || signal.status === 'missed' || signal.status === 'cancelled')
}
```

- [ ] **Step 4: Run targeted tests and confirm GREEN**

Run the same targeted command. Expected: all tests pass with zero warnings.

- [ ] **Step 5: Commit the trust boundary**

```bash
git add front/src/lib/workbenchViewModel.ts front/src/lib/workbenchViewModel.test.ts front/src/lib/dashboard.ts front/src/lib/dashboard.test.ts
git commit -m "fix: unify workbench truth and opportunity states"
```

---

### Task 2: Integrate Canonical Data and Accessible Chart Summary

**Files:**

- Create: `src/components/workbench/ChartAccessibleSummary.tsx`
- Modify: `src/App.tsx`
- Modify: `src/App.test.tsx`
- Modify: `src/pages/HomeDashboard.tsx`
- Modify: `src/pages/ThemePage.tsx`
- Modify: `src/components/MarketHeader.tsx`
- Modify: `src/components/charts/PerformanceChart.tsx`

**Interfaces:**

- Consumes: `WorkbenchViewModel` from Task 1.
- Produces: one canonical `workbench` prop for homepage and theme pages; `PerformanceChart` accepts `ariaLabel?: string` and always renders `ChartAccessibleSummary`.

- [ ] **Step 1: Add failing integration and accessibility tests**

Add an `App.test.tsx` snapshot response whose raw `portfolio.returnPct` is `-0.03`, whose `marketSummaries` aggregate is `+1.13`, and whose final raw performance point is `-0.03`. Assert:

```ts
await waitFor(() => expect(screen.getAllByText('+1.13%').length).toBeGreaterThan(0))
expect(screen.getByLabelText('收益曲线摘要')).toHaveTextContent('当前收益 +1.13%')
expect(screen.queryByText('当前收益 -0.03%')).not.toBeInTheDocument()
```

Add a chart accessibility assertion:

```ts
expect(screen.getByRole('img', { name: '模拟盘收益曲线' })).toBeInTheDocument()
```

- [ ] **Step 2: Run the integration test and confirm RED**

```bash
npm test -- --run src/App.test.tsx
```

Expected: FAIL because the chart has no accessible name/summary and theme/home surfaces still derive different latest values.

- [ ] **Step 3: Wire the canonical model through App**

In `App.tsx`, derive `workbench` once with `useMemo()` from primitive references already in scope. Replace separate `visiblePortfolio`, `visiblePerformanceData`, `visibleSignals`, and `visibleHoldings` consumer props with `workbench` fields. Keep existing `domainStatus` and retry behavior unchanged.

Create `ChartAccessibleSummary.tsx`:

```tsx
import type { PerformancePoint } from '../../types/dashboard'

export function ChartAccessibleSummary({ latest }: { latest: PerformancePoint | null }) {
  if (!latest) return <p className="sr-only" aria-label="收益曲线摘要">历史曲线尚未形成</p>
  return (
    <p className="sr-only" aria-label="收益曲线摘要">
      当前收益 {latest.simulated >= 0 ? '+' : ''}{latest.simulated.toFixed(2)}%，
      目标 {latest.target >= 0 ? '+' : ''}{latest.target.toFixed(2)}%，
      市场基准 {latest.benchmark >= 0 ? '+' : ''}{latest.benchmark.toFixed(2)}%。
    </p>
  )
}
```

Wrap the chart container with `role="img"` and `aria-label={ariaLabel ?? '模拟盘收益曲线'}`; render the summary next to the Recharts canvas. Remove page-local latest-point patching from `HomeDashboard` because the model owns it.

- [ ] **Step 4: Run App tests and confirm GREEN**

```bash
npm test -- --run src/App.test.tsx src/lib/workbenchViewModel.test.ts
```

Expected: all targeted tests pass.

- [ ] **Step 5: Commit canonical UI integration**

```bash
git add front/src/App.tsx front/src/App.test.tsx front/src/pages/HomeDashboard.tsx front/src/pages/ThemePage.tsx front/src/components/MarketHeader.tsx front/src/components/charts/PerformanceChart.tsx front/src/components/workbench/ChartAccessibleSummary.tsx
git commit -m "refactor: drive dashboard from canonical workbench data"
```

---

### Task 3: Continuous Workbench Shell and Blotter

**Files:**

- Create: `src/components/workbench/WorkbenchShell.tsx`
- Create: `src/components/workbench/ReviewRail.tsx`
- Create: `src/components/workbench/WorkbenchBlotter.tsx`
- Create: `src/components/workbench/WorkbenchBlotter.test.tsx`
- Modify: `src/pages/HomeDashboard.tsx`
- Modify: `src/components/tables/OpportunityTable.tsx`
- Modify: `src/components/tables/HoldingsTable.tsx`
- Modify: `src/components/tables/SignalTable.tsx`

**Interfaces:**

```ts
export type BlotterTab = 'active' | 'positions' | 'completed' | 'review'

export function WorkbenchBlotter(props: {
  active: SignalRow[]
  positions: HoldingRow[]
  completed: SignalRow[]
  review: SignalRow[]
}): JSX.Element

export function ReviewRail(props: {
  active: SignalRow[]
  positions: HoldingRow[]
  review: SignalRow[]
  portfolio: PortfolioSummary | null
}): JSX.Element
```

- [ ] **Step 1: Write failing blotter tests**

Create `WorkbenchBlotter.test.tsx`:

```tsx
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkbenchBlotter } from './WorkbenchBlotter'
import type { HoldingRow, SignalRow } from '../../types/dashboard'

const pending: SignalRow = { symbol: '0700.HK', name: '腾讯', market: 'HK', method: '事件', status: 'pending', impact: '--', confidence: '80%', age: '1小时', reason: '等待确认', next: '继续观察', steps: 4 }
const executed: SignalRow = { ...pending, symbol: 'AAPL.US', name: '苹果', market: 'US', status: 'executed', reason: '已经形成结果' }
const holding: HoldingRow = { symbol: '600519.SH', name: '贵州茅台', market: 'A-share', weight: '¥1万', pnl: '+¥20', risk: '正常', role: '模拟盘持仓' }

describe('WorkbenchBlotter', () => {
  it('starts on active opportunities without terminal rows', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)
    const panel = screen.getByRole('tabpanel', { name: '当前机会' })
    expect(within(panel).getByText('0700.HK')).toBeInTheDocument()
    expect(within(panel).queryByText('AAPL.US')).not.toBeInTheDocument()
  })

  it('shows a truthful empty state instead of completed rows', () => {
    render(<WorkbenchBlotter active={[]} positions={[holding]} completed={[executed]} review={[]} />)
    expect(screen.getByText('当前没有待处理机会')).toBeInTheDocument()
    expect(screen.queryByText('AAPL.US')).not.toBeInTheDocument()
  })

  it('switches to completed outcomes', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)
    fireEvent.click(screen.getByRole('tab', { name: '已完成 1' }))
    expect(screen.getByRole('tabpanel', { name: '已完成' })).toHaveTextContent('AAPL.US')
  })
})
```

- [ ] **Step 2: Run blotter tests and confirm RED**

```bash
npm test -- --run src/components/workbench/WorkbenchBlotter.test.tsx
```

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement workbench components**

Implement `WorkbenchBlotter` with a local `useState<BlotterTab>('active')`, one `role="tablist"`, four tabs with counts, and one labelled `role="tabpanel"`. Render existing tables for populated tabs and exact empty copy for empty tabs.

Implement `ReviewRail` priority:

1. first blocked/review signal;
2. first active opportunity;
3. first risk-high position;
4. first position;
5. empty review state.

Its only actions are navigation callbacks such as `查看机会`, `查看持仓`, or `进入复盘`; no execution verbs.

Implement `WorkbenchShell` markup:

```tsx
<section className="workbench-shell" aria-label="交易工作台">
  <div className="workbench-primary">
    <section className="workbench-chart" aria-label="收益与目标">
      {chart}
    </section>
    <ReviewRail {...reviewProps} />
  </div>
  <div className="workbench-funnel-context">{funnel}</div>
  <WorkbenchBlotter {...blotterProps} />
</section>
```

Replace the homepage's separate hero, drilldown, and equal-weight rail stack with `WorkbenchShell`. Keep A-share evidence modules below the blotter as secondary evidence, not in the first-screen rail.

- [ ] **Step 4: Run component and App tests and confirm GREEN**

```bash
npm test -- --run src/components/workbench/WorkbenchBlotter.test.tsx src/App.test.tsx
```

Expected: all targeted tests pass after updating existing homepage assertions to the new region labels.

- [ ] **Step 5: Commit the shell**

```bash
git add front/src/components/workbench front/src/pages/HomeDashboard.tsx front/src/components/tables/OpportunityTable.tsx front/src/components/tables/HoldingsTable.tsx front/src/components/tables/SignalTable.tsx front/src/App.test.tsx
git commit -m "feat: add continuous read-only trading workbench"
```

---

### Task 4: Live Gate, Runtime Copy, and Empty Attribution

**Files:**

- Modify: `src/App.test.tsx`
- Modify: `src/components/LiveGate.tsx`
- Modify: `src/components/workbench/WorkbenchShell.tsx`
- Modify: `src/components/panels/ClosedLoopProofPanel.tsx`
- Modify: `src/components/charts/ContributionPanel.tsx`
- Modify: `src/lib/workbenchViewModel.ts`
- Modify: `src/lib/workbenchViewModel.test.ts`

**Interfaces:**

- Uses `WorkbenchViewModel.liveGate` and `formatRuntimeReason()` from Task 1.
- Produces a dedicated `实盘待接入` workbench state and a real attribution empty state.

- [ ] **Step 1: Write failing live-gate and copy tests**

In `App.test.tsx`:

```ts
click(screen.getByRole('tab', { name: '实盘' }))
expect(screen.getByRole('region', { name: '实盘接入状态' })).toHaveTextContent('实盘待接入')
expect(screen.getByText('模拟盘参考')).toBeInTheDocument()
expect(screen.queryByText('market_data_missing')).not.toBeInTheDocument()
expect(screen.queryByRole('button', { name: /买|卖|下单|确认交易/ })).not.toBeInTheDocument()
```

Add a `ContributionPanel` test or App assertion that `impact: '--'` produces `暂无可用收益归因` and does not render a zero-value `buy` chart.

- [ ] **Step 2: Run tests and confirm RED**

```bash
npm test -- --run src/App.test.tsx src/lib/workbenchViewModel.test.ts
```

Expected: FAIL because live mode currently swaps labels in place and raw runtime reasons may still render.

- [ ] **Step 3: Implement the dedicated gate and copy normalization**

Render `LiveGate` as a `section role="region" aria-label="实盘接入状态"` inside the workbench primary region. Keep the current selected market visible as `模拟盘参考`, but replace result amount, review actions, and blotter content with connection requirements.

Replace `ClosedLoopProofPanel.normalizeReason()` with `formatRuntimeReason()`. The formatter returns Chinese copy for known reasons and `等待更多市场信息` for unknown underscore-separated backend codes instead of echoing the raw value.

In `ContributionPanel`, compute only finite non-zero impacts. When none exist, render:

```tsx
<div className="empty-panel-copy" aria-label="收益归因状态">
  <strong>暂无可用收益归因</strong>
  <span>产生带明确收益贡献的复盘记录后，这里再展示排名。</span>
</div>
```

- [ ] **Step 4: Run targeted tests and confirm GREEN**

Run the same command. Expected: all targeted tests pass.

- [ ] **Step 5: Commit gate and copy changes**

```bash
git add front/src/App.test.tsx front/src/components/LiveGate.tsx front/src/components/workbench/WorkbenchShell.tsx front/src/components/panels/ClosedLoopProofPanel.tsx front/src/components/charts/ContributionPanel.tsx front/src/lib/workbenchViewModel.ts front/src/lib/workbenchViewModel.test.ts
git commit -m "fix: gate live mode and normalize dashboard copy"
```

---

### Task 5: Hyperliquid-Inspired Desktop Visual Hierarchy

**Files:**

- Modify: `src/App.test.tsx`
- Modify: `src/App.css`
- Modify: `src/index.css`
- Modify: `src/styles/home-funnel.css`
- Modify: `src/styles/page-summary.css`
- Modify: `src/components/MarketHeader.tsx`
- Modify: `src/components/TopNav.tsx`
- Modify: `src/components/workbench/WorkbenchShell.tsx`
- Modify: `src/components/workbench/ReviewRail.tsx`
- Modify: `src/components/workbench/WorkbenchBlotter.tsx`

**Interfaces:**

- Consumes the semantic workbench regions from Tasks 2-4.
- Produces a 1280x720 and 1440x900 continuous desktop workbench with no first-screen overlap.

- [ ] **Step 1: Add a failing structural hierarchy test**

In `App.test.tsx`, assert the homepage contains one workbench and its ordered regions:

```ts
const workbench = screen.getByRole('region', { name: '交易工作台' })
expect(within(workbench).getByRole('region', { name: '收益与目标' })).toBeInTheDocument()
expect(within(workbench).getByRole('complementary', { name: '当前审阅' })).toBeInTheDocument()
expect(within(workbench).getByRole('tablist', { name: '工作台明细' })).toBeInTheDocument()
expect(screen.getAllByLabelText('交易工作台')).toHaveLength(1)
```

- [ ] **Step 2: Run the structural test and confirm RED**

```bash
npm test -- --run src/App.test.tsx
```

Expected: FAIL until region labels and final composition exist.

- [ ] **Step 3: Apply the desktop visual system**

Use existing tokens and add only the missing semantic aliases:

```css
:root {
  --surface-elevated: #0b1516;
  --border-strong: rgba(210, 225, 220, 0.16);
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
}
```

Implement the desktop geometry:

```css
.workspace { min-height: calc(100vh - 164px); }
.workbench-shell { margin: 10px 40px 28px; border: 1px solid var(--border-workbench); background: var(--surface-workbench); }
.workbench-primary { display: grid; grid-template-columns: minmax(0, 1fr) 320px; min-height: 392px; }
.workbench-chart { min-width: 0; border-right: 1px solid var(--border-hairline); }
.workbench-review-rail { min-width: 0; background: color-mix(in srgb, var(--surface-workbench) 86%, black); }
.workbench-funnel-context { border-top: 1px solid var(--border-hairline); border-bottom: 1px solid var(--border-hairline); }
.workbench-blotter { min-height: 246px; }
```

Compact the market header to a Hyperliquid-like strip, keep the selected market and freshness on the first row, and keep six canonical metrics on the second row. Demote the funnel to a 76-112px context strip. Use cyan only for selected state and positive results, amber for review/waiting, and red for negative/risk.

Do not add mobile CSS. Preserve current `@media` rules unless a selector rename requires compatibility updates.

- [ ] **Step 4: Run App tests and visual build checks**

```bash
npm test -- --run src/App.test.tsx src/components/workbench/WorkbenchBlotter.test.tsx
npm run lint
npm run build
```

Expected: tests pass, lint exits 0, and Vite build exits 0 without new dependency or TypeScript errors.

- [ ] **Step 5: Commit desktop styling**

```bash
git add front/src/App.test.tsx front/src/App.css front/src/index.css front/src/styles/home-funnel.css front/src/styles/page-summary.css front/src/components/MarketHeader.tsx front/src/components/TopNav.tsx front/src/components/workbench
git commit -m "style: refine desktop trading workbench hierarchy"
```

---

### Task 6: Documentation, Full Verification, and Browser QA

**Files:**

- Modify: `README.md`
- Modify: `DESIGN.md`
- Modify: `../STATUS.md`
- Verify: all frontend source and tests changed above.

**Interfaces:**

- Consumes the completed workbench implementation.
- Produces durable handoff docs, a scorecard, screenshots, and a verified candidate branch.

- [ ] **Step 1: Update durable documentation**

Document these exact behaviors:

- one canonical selected-view portfolio drives header and chart;
- active opportunities exclude terminal results;
- desktop homepage is market strip + chart + review rail + blotter;
- live mode remains gated and read-only;
- mobile work is intentionally deferred;
- no snapshot API contract changed.

Update `DESIGN.md` with the final Design Taste score across hierarchy, typography, color semantics, spacing, feedback, accessibility, brand fit, and responsive integrity. Do not score responsive integrity above 3/5 because mobile is deferred.

- [ ] **Step 2: Run the full required command set**

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
```

Expected: all commands exit 0; tests report zero failures.

- [ ] **Step 3: Start the local app and run Browser QA**

Run `npm run dev -- --host 127.0.0.1` and use the in-app Browser at `http://127.0.0.1:5173/`.

Validate at 1280x720 and 1440x900:

- correct page identity and non-blank content;
- no framework error overlay;
- no relevant console error/warn;
- market strip, chart, review rail, and blotter visible without overlap;
- header and chart summary show the same current return;
- active opportunities contain no terminal rows;
- blotter tab interaction changes the visible table;
- live mode shows `实盘待接入` and no execution controls;
- raw backend runtime codes are absent.

Save screenshots outside the repository under `/tmp/tradingagent-workbench-20260711/`.

- [ ] **Step 4: Review the final diff and acceptance criteria**

```bash
git diff --check
git status --short
git diff --stat origin/main...HEAD
```

Re-read the approved spec and confirm each acceptance criterion with a test, command, screenshot, or named residual gap.

- [ ] **Step 5: Commit documentation and verification handoff**

```bash
git add front/README.md front/DESIGN.md STATUS.md
git commit -m "docs: hand off read-only workbench validation"
```

Do not push or deploy. Finish through `superpowers:finishing-a-development-branch` and present the verified integration options to Nicholas.
