import { PerformanceChart } from '../components/charts/PerformanceChart'
import { useMemo } from 'react'
import { ChartSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { AShareEvidencePanel } from '../components/panels/AShareEvidencePanel'
import { AShareMoneyflowPanel } from '../components/panels/AShareMoneyflowPanel'
import { ClosedLoopProofPanel } from '../components/panels/ClosedLoopProofPanel'
import { HoldingsCompact } from '../components/panels/HoldingsCompact'
import { HomeResultBrief } from '../components/panels/HomeResultBrief'
import { MarketSummaryPanel } from '../components/panels/MarketSummaryPanel'
import { OpportunityFocus } from '../components/panels/OpportunityFocus'
import { RealtimeReturnCard } from '../components/panels/RealtimeReturnCard'
import { SignalFunnelFlow } from '../components/panels/SignalFunnelFlow'
import { formatTime } from '../lib/format'
import { getSignalFunnel } from '../lib/dashboard'
import type { AShareForwardValidation, AShareResearchEvidence, AccountMode, ChartEvent, FunnelEvent, HoldingRow, Market, MarketSummary, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function HomeDashboard({
  accountMode,
  ashareForwardValidation,
  activeMarket,
  ashareResearchEvidence,
  data,
  latestPoint,
  hasHoldingData,
  hasPerformanceData,
  hasSignalData,
  holdings,
  marketSummary,
  marketSummaries,
  now,
  portfolio,
  domainStatus,
  onRetry,
  selectAccountMode,
  setActivePage,
  signals,
  funnelEvents,
  events,
}: {
  accountMode: AccountMode
  activeMarket: Market
  ashareForwardValidation?: AShareForwardValidation
  ashareResearchEvidence?: AShareResearchEvidence
  data: PerformancePoint[]
  events: ChartEvent[]
  hasHoldingData: boolean
  hasPerformanceData: boolean
  hasSignalData: boolean
  holdings: HoldingRow[]
  marketSummary?: MarketSummary
  marketSummaries: MarketSummary[]
  latestPoint: PerformancePoint
  now: Date
  portfolio: PortfolioSummary | null
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  signals: SignalRow[]
  funnelEvents: FunnelEvent[]
}) {
  const signalFunnel = getSignalFunnel(signals)
  const liveProfit = portfolio?.pnlAmount ?? 0
  const liveReturn = portfolio?.returnPct ?? latestPoint.simulated
  const targetReturn = portfolio?.targetPct ?? latestPoint.target
  const targetGap = liveReturn - targetReturn
  const returnChartData = useMemo(() => {
    if (!portfolio || data.length === 0) return data
    return data.map((point, index) => index === data.length - 1
      ? {
          ...point,
          simulated: portfolio.returnPct,
          target: portfolio.targetPct,
        }
      : point)
  }, [data, portfolio])
  const returnChartLatestPoint = returnChartData[returnChartData.length - 1] ?? latestPoint
  const headline = hasPerformanceData
    ? targetGap >= 0
      ? '当前收益领先目标，回撤仍在边界内。'
      : '收益暂时落后目标，先看机会质量和风险距离。'
    : '收益结果还没有写入，先看机会和持仓。'

  return (
    <div className="home-layout">
      <section className="home-main">
        <section className="panel performance-panel hero-performance">
          <div className="performance-headline">
            <SignalFunnelFlow events={funnelEvents} hasSignalData={hasSignalData} holdings={holdings} signals={signals} />
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
          <StatusBoundary loading={<ChartSkeleton height={236} />} onRetry={onRetry} status={hasPerformanceData ? domainStatus('performance') : 'ready'}>
            {hasPerformanceData ? (
              <PerformanceChart data={returnChartData} events={events} height={236} latestPoint={returnChartLatestPoint} onSelectEvent={setActivePage} />
            ) : (
              <div className="chart-empty-state" style={{ height: 236 }}>
                <span>等待收益序列</span>
                <strong>连接正常，暂无可展示的收益曲线。</strong>
                <p>当模拟盘写入净值、目标和市场基准后，这里会自动更新。</p>
              </div>
            )}
          </StatusBoundary>
          <div className="chart-meta">
            <span>{formatTime(now)} (UTC+8)</span>
            <b>{hasPerformanceData ? '已更新' : '等待数据'}</b>
            <em>{hasPerformanceData ? `机会差 ${returnChartLatestPoint.opportunity.toFixed(2)}%` : '未显示样例收益'}</em>
          </div>
        </section>

        <section className="home-drilldown" aria-label="当前机会和持仓结果">
          <div className="drilldown-header">
            <span>机会和持仓</span>
            <strong>已接入快照时只显示真实记录</strong>
          </div>
          <div className="home-support-grid">
            <OpportunityFocus hasSignalData={hasSignalData} setActivePage={setActivePage} signals={signals} />
            <HoldingsCompact hasHoldingData={hasHoldingData} holdings={holdings} setActivePage={setActivePage} />
          </div>
        </section>
      </section>

      <aside className="home-rail">
        <MarketSummaryPanel activeMarket={activeMarket} summary={marketSummary} />
        <ClosedLoopProofPanel summaries={marketSummaries} />
        <AShareMoneyflowPanel activeMarket={activeMarket} signals={signals} />
        <HomeResultBrief hasHoldingData={hasHoldingData} hasPerformanceData={hasPerformanceData} hasSignalData={hasSignalData} holdings={holdings} portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
        {(activeMarket === 'All Markets' || activeMarket === 'A-share') && <AShareEvidencePanel evidence={ashareResearchEvidence} forwardValidation={ashareForwardValidation} />}
      </aside>
    </div>
  )
}
