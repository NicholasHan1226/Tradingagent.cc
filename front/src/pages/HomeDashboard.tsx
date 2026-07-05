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
import type { AccountMode, ChartEvent, HoldingRow, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function HomeDashboard({
  accountMode,
  data,
  latestPoint,
  hasHoldingData,
  hasPerformanceData,
  hasSignalData,
  holdings,
  now,
  portfolio,
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
  hasHoldingData: boolean
  hasPerformanceData: boolean
  hasSignalData: boolean
  holdings: HoldingRow[]
  latestPoint: PerformancePoint
  now: Date
  portfolio: PortfolioSummary | null
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const signalFunnel = getSignalFunnel(signals)
  const liveProfit = portfolio?.pnlAmount ?? 0
  const liveReturn = portfolio?.returnPct ?? latestPoint.simulated
  const targetReturn = portfolio?.targetPct ?? latestPoint.target
  const targetGap = liveReturn - targetReturn
  const headline = hasPerformanceData
    ? targetGap >= 0
      ? '模拟盘高于目标运行。'
      : '模拟盘低于目标，先看机会质量和风险距离。'
    : '暂无收益结果，先保持为空。'

  return (
    <div className="home-layout">
      <section className="home-main">
        <section className="panel performance-panel hero-performance">
          <div className="performance-headline">
            <SignalFunnelFlow hasSignalData={hasSignalData} signals={signals} />
            <RealtimeReturnCard
              accountMode={accountMode}
              executedCount={signalFunnel.executed.length}
              hasPerformanceData={hasPerformanceData}
              headline={headline}
              liveProfit={liveProfit}
              liveReturn={liveReturn}
              missedCount={signalFunnel.missed.length}
              pendingCount={signalFunnel.pending.length}
              portfolio={portfolio}
              selectAccountMode={selectAccountMode}
              setActivePage={setActivePage}
              targetReturn={targetReturn}
            />
          </div>
          <div className="chart-section-title">
            <span>收益曲线</span>
            <strong>{hasPerformanceData ? '持续性与风险距离' : '等待收益、目标和基准数据'}</strong>
          </div>
          <StatusBoundary loading={<ChartSkeleton height={316} />} onRetry={onRetry} status={hasPerformanceData ? domainStatus('performance') : 'ready'}>
            {hasPerformanceData ? (
              <PerformanceChart data={data} events={events} height={316} latestPoint={latestPoint} onSelectEvent={setActivePage} />
            ) : (
              <div className="chart-empty-state" style={{ height: 316 }}>
                <span>等待收益序列</span>
                <strong>连接正常，暂无可展示的收益曲线。</strong>
                <p>当模拟盘写入净值、目标和市场基准后，这里会自动更新。</p>
              </div>
            )}
          </StatusBoundary>
          <div className="chart-meta">
            <span>{formatTime(now)} (UTC+8)</span>
            <b>{hasPerformanceData ? '已更新' : '等待数据'}</b>
            <em>{hasPerformanceData ? `机会偏差 ${latestPoint.opportunity.toFixed(2)}%` : '未显示样例收益'}</em>
          </div>
        </section>

        <section className="home-drilldown" aria-label="当前机会和持仓结果">
          <div className="drilldown-header">
            <span>机会 / 持仓结果</span>
            <strong>只展示已有记录，不用样例补位</strong>
          </div>
          <div className="home-support-grid">
            <OpportunityFocus hasSignalData={hasSignalData} setActivePage={setActivePage} signals={signals} />
            <HoldingsCompact hasHoldingData={hasHoldingData} holdings={holdings} setActivePage={setActivePage} />
          </div>
        </section>
      </section>

      <aside className="home-rail">
        <HomeResultBrief hasHoldingData={hasHoldingData} hasPerformanceData={hasPerformanceData} hasSignalData={hasSignalData} holdings={holdings} portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
      </aside>
    </div>
  )
}
