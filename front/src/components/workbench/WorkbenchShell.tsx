import { useState, type ReactNode } from 'react'
import type { AutomationRuntimeItem } from '../../lib/automationObservatoryViewModel'
import type { HoldingRow, SignalRow } from '../../types/dashboard'
import { LiveGate } from '../LiveGate'
import { RuntimeRail } from './RuntimeRail'
import { WorkbenchBlotter, type BlotterTab } from './WorkbenchBlotter'

export function WorkbenchShell({
  active,
  chart,
  completed,
  context,
  evidence,
  liveGate,
  onUseSimulation,
  positions,
  review,
  runningCount,
  runtimeItem,
}: {
  active: SignalRow[]
  chart: ReactNode
  completed: SignalRow[]
  context: ReactNode
  evidence?: ReactNode
  liveGate: { gated: boolean; title: string; detail: string }
  onUseSimulation: () => void
  positions: HoldingRow[]
  review: SignalRow[]
  runningCount: number
  runtimeItem: AutomationRuntimeItem
}) {
  const [blotterTab, setBlotterTab] = useState<BlotterTab>('active')

  return (
    <section className="workbench-shell" aria-label="交易工作台">
      {liveGate.gated ? (
        <div className="workbench-live-state">
          <LiveGate detail={liveGate.detail} onUseSimulation={onUseSimulation} title={liveGate.title} />
        </div>
      ) : (
        <>
          <div className="workbench-primary-grid">
            <section className="workbench-chart-region" aria-label="收益与目标">
              {chart}
            </section>
            <RuntimeRail item={runtimeItem} runningCount={runningCount} />
          </div>
          <div className="workbench-context-strip">{context}</div>
          <WorkbenchBlotter
            active={active}
            completed={completed}
            onTabChange={setBlotterTab}
            positions={positions}
            review={review}
            selectedTab={blotterTab}
          />
          {evidence && <div className="workbench-evidence">{evidence}</div>}
        </>
      )}
    </section>
  )
}
