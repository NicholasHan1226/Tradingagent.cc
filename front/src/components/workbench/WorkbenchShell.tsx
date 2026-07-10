import type { ReactNode } from 'react'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { LiveGate } from '../LiveGate'
import { ReviewRail } from './ReviewRail'
import { WorkbenchBlotter } from './WorkbenchBlotter'

export function WorkbenchShell({
  active,
  chart,
  completed,
  context,
  evidence,
  liveGate,
  onUseSimulation,
  portfolio,
  positions,
  review,
  setActivePage,
}: {
  active: SignalRow[]
  chart: ReactNode
  completed: SignalRow[]
  context: ReactNode
  evidence?: ReactNode
  liveGate: { gated: boolean; title: string; detail: string }
  onUseSimulation: () => void
  portfolio: PortfolioSummary | null
  positions: HoldingRow[]
  review: SignalRow[]
  setActivePage: (page: Page) => void
}) {
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
            <ReviewRail
              active={active}
              positions={positions}
              portfolio={portfolio}
              review={review}
              setActivePage={setActivePage}
            />
          </div>
          <div className="workbench-context-strip">{context}</div>
          <WorkbenchBlotter active={active} completed={completed} positions={positions} review={review} />
          {evidence && <div className="workbench-evidence">{evidence}</div>}
        </>
      )}
    </section>
  )
}
