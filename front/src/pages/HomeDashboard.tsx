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
import type { AccountMode, ChartEvent, HoldingRow, Page, PerformancePoint, SignalRow } from '../types/dashboard'
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
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const signalFunnel = getSignalFunnel(signals)
  const simulatedCapitalBase = 1365336.73
  const liveProfit = simulatedCapitalBase * latestPoint.simulated / 100
  const headline = hasPerformanceData
    ? '收益保持在目标上方，风险仍在边界内。'
    : '收益通道已连接，等待交易系统写入最新曲线。'

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

        <section className="home-drilldown" aria-label="当前机会和持仓">
          <div className="drilldown-header">
            <span>当前结果</span>
            <strong>机会与持仓只展示可用记录</strong>
          </div>
          <div className="home-support-grid">
            <OpportunityFocus hasSignalData={hasSignalData} setActivePage={setActivePage} signals={signals} />
            <HoldingsCompact hasHoldingData={hasHoldingData} holdings={holdings} setActivePage={setActivePage} />
          </div>
        </section>
      </section>

      <aside className="home-rail">
        <HomeResultBrief hasHoldingData={hasHoldingData} hasPerformanceData={hasPerformanceData} hasSignalData={hasSignalData} holdings={holdings} setActivePage={setActivePage} signals={signals} />
      </aside>
    </div>
  )
}
