import { PerformanceChart } from '../components/charts/PerformanceChart'
import { ChartSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { HoldingsCompact } from '../components/panels/HoldingsCompact'
import { HomeResultBrief } from '../components/panels/HomeResultBrief'
import { OpportunityFocus } from '../components/panels/OpportunityFocus'
import { RealtimeReturnCard } from '../components/panels/RealtimeReturnCard'
import { SignalFunnelFlow } from '../components/panels/SignalFunnelFlow'
import { formatTime } from '../lib/format'
import { getSignalFunnel } from '../lib/dashboard'
import type { AccountMode, ChartEvent, Page, PerformancePoint, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function HomeDashboard({
  accountMode,
  data,
  latestPoint,
  now,
  domainStatus,
  onRetry,
  selectAccountMode,
  setActivePage,
  signals,
  events,
}: {
  accountMode: AccountMode
  data: PerformancePoint[]
  events: ChartEvent[]
  latestPoint: PerformancePoint
  now: Date
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const signalFunnel = getSignalFunnel(signals)
  const simulatedCapitalBase = 1365336.73
  const liveProfit = simulatedCapitalBase * latestPoint.simulated / 100

  return (
    <div className="home-layout">
      <section className="home-main">
        <section className="panel performance-panel hero-performance">
          <div className="performance-headline">
            <SignalFunnelFlow signals={signals} />
            <RealtimeReturnCard
              accountMode={accountMode}
              executedCount={signalFunnel.executed.length}
              liveProfit={liveProfit}
              liveReturn={latestPoint.simulated}
              missedCount={signalFunnel.missed.length}
              pendingCount={signalFunnel.pending.length}
              selectAccountMode={selectAccountMode}
              setActivePage={setActivePage}
              targetReturn={latestPoint.target}
            />
          </div>
          <div className="chart-section-title">
            <span>收益曲线</span>
            <strong>持续性与风险距离</strong>
          </div>
          <StatusBoundary loading={<ChartSkeleton height={316} />} onRetry={onRetry} status={domainStatus('performance')}>
            <PerformanceChart data={data} events={events} height={316} latestPoint={latestPoint} onSelectEvent={setActivePage} />
          </StatusBoundary>
          <div className="chart-meta">
            <span>{formatTime(now)} (UTC+8)</span>
            <b>实时</b>
            <em>机会偏差 {latestPoint.opportunity.toFixed(2)}%</em>
          </div>
        </section>

        <section className="home-drilldown" aria-label="收益来源">
          <div className="drilldown-header">
            <span>收益来源</span>
            <strong>机会 · 持仓 · 风险</strong>
          </div>
          <div className="home-support-grid">
            <OpportunityFocus setActivePage={setActivePage} signals={signals} />
            <HoldingsCompact setActivePage={setActivePage} />
          </div>
        </section>
      </section>

      <aside className="home-rail">
        <HomeResultBrief setActivePage={setActivePage} signals={signals} />
      </aside>
    </div>
  )
}
