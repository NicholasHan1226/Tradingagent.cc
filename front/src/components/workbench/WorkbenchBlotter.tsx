import { useState } from 'react'
import { HoldingsTable } from '../tables/HoldingsTable'
import { RunningProcessTable } from '../tables/RunningProcessTable'
import { SignalTable } from '../tables/SignalTable'
import type { HoldingRow, SignalRow } from '../../types/dashboard'
import { getPreferredTab } from './workbenchBlotterState'

export type BlotterTab = 'active' | 'positions' | 'completed' | 'review'

const TAB_LABELS: Record<BlotterTab, string> = {
  active: '运行中',
  positions: '持仓',
  completed: '已完成',
  review: '自动复盘',
}

export function WorkbenchBlotter({
  active,
  positions,
  completed,
  review,
  selectedTab,
  onTabChange,
}: {
  active: SignalRow[]
  positions: HoldingRow[]
  completed: SignalRow[]
  review: SignalRow[]
  selectedTab?: BlotterTab
  onTabChange?: (tab: BlotterTab) => void
}) {
  const [internalTab, setInternalTab] = useState<BlotterTab>(() => getPreferredTab({ active, positions, completed, review }))
  const tab = selectedTab ?? internalTab
  const selectTab = (nextTab: BlotterTab) => {
    setInternalTab(nextTab)
    onTabChange?.(nextTab)
  }
  const counts: Record<BlotterTab, number> = {
    active: active.length,
    positions: positions.length,
    completed: completed.length,
    review: review.length,
  }

  return (
    <section className="workbench-blotter" aria-label="工作台明细区">
      <div className="workbench-blotter-tabs" aria-label="工作台明细" role="tablist">
        {(Object.keys(TAB_LABELS) as BlotterTab[]).map((key) => (
          <button
            aria-controls={`blotter-panel-${key}`}
            aria-selected={tab === key}
            className={tab === key ? 'selected' : ''}
            id={`blotter-tab-${key}`}
            key={key}
            onClick={() => selectTab(key)}
            role="tab"
            type="button"
          >
            {TAB_LABELS[key]} {counts[key]}
          </button>
        ))}
      </div>
      <div
        aria-label={TAB_LABELS[tab]}
        className="workbench-blotter-panel"
        id={`blotter-panel-${tab}`}
        role="tabpanel"
      >
        {renderPanel(tab, { active, positions, completed, review })}
      </div>
    </section>
  )
}

function renderPanel(
  tab: BlotterTab,
  rows: { active: SignalRow[]; positions: HoldingRow[]; completed: SignalRow[]; review: SignalRow[] },
) {
  if (tab === 'active') {
    return rows.active.length
      ? <RunningProcessTable signals={rows.active} />
      : <EmptyState title="当前没有运行中的自动过程" detail="系统等待下一轮调度，持仓与历史结果继续保留。" />
  }
  if (tab === 'positions') {
    return rows.positions.length
      ? <HoldingsTable holdings={rows.positions} />
      : <EmptyState title="暂无持仓记录" detail="模拟盘形成持仓后，这里会显示仓位、收益和风险。" />
  }
  if (tab === 'completed') {
    return rows.completed.length
      ? <SignalTable signals={rows.completed} />
      : <EmptyState title="暂无已完成结果" detail="成交、错过或取消记录会进入这里。" />
  }
  return rows.review.length
    ? <SignalTable signals={rows.review} />
    : <EmptyState title="当前没有自动复盘记录" detail="风险拦截、异常和结果归因会在这里集中展示。" />
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-panel-copy workbench-empty-state">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  )
}
