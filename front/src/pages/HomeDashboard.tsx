import { PerformanceChart } from '../components/charts/PerformanceChart'
import { ChartSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { AShareEvidencePanel } from '../components/panels/AShareEvidencePanel'
import { AShareMoneyflowPanel } from '../components/panels/AShareMoneyflowPanel'
import { AShareTierComparisonPanel } from '../components/panels/AShareTierComparisonPanel'
import { ClosedLoopProofPanel } from '../components/panels/ClosedLoopProofPanel'
import { HomeResultBrief } from '../components/panels/HomeResultBrief'
import { MarketSummaryPanel } from '../components/panels/MarketSummaryPanel'
import { RealtimeReturnCard } from '../components/panels/RealtimeReturnCard'
import { SignalFunnelFlow } from '../components/panels/SignalFunnelFlow'
import { WorkbenchShell } from '../components/workbench/WorkbenchShell'
import { formatTime } from '../lib/format'
import { getSignalFunnel } from '../lib/dashboard'
import type { AShareForwardValidation, AShareResearchEvidence, AShareTierSummary, AccountMode, ChartEvent, FunnelEvent, HoldingRow, Market, MarketSummary, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function HomeDashboard({
  accountMode,
  ashareForwardValidation,
  activeMarket,
  ashareResearchEvidence,
  ashareTierSummaries,
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
  activeSignals,
  completedSignals,
  reviewItems,
  liveGate,
}: {
  accountMode: AccountMode
  activeMarket: Market
  ashareForwardValidation?: AShareForwardValidation
  ashareResearchEvidence?: AShareResearchEvidence
  ashareTierSummaries?: AShareTierSummary[]
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
  activeSignals: SignalRow[]
  completedSignals: SignalRow[]
  reviewItems: SignalRow[]
  liveGate: { gated: boolean; title: string; detail: string }
}) {
  const signalFunnel = getSignalFunnel(signals)
  const liveProfit = portfolio?.pnlAmount ?? 0
  const liveReturn = portfolio?.returnPct ?? latestPoint.simulated
  const targetReturn = portfolio?.targetPct ?? latestPoint.target
  const targetGap = liveReturn - targetReturn
  const returnTone = getReturnTone(liveProfit, liveReturn)
  const returnChartLatestPoint = data[data.length - 1] ?? latestPoint
  const headline = hasPerformanceData
    ? targetGap >= 0
      ? '当前收益领先目标，回撤仍在边界内。'
      : '收益暂时落后目标，先看机会质量和风险距离。'
    : '收益结果还没有写入，先看机会和持仓。'

  const chart = (
    <section className="panel performance-panel hero-performance">
          <div className="performance-headline">
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
              <PerformanceChart
                currentTone={returnTone}
                data={data}
                events={events}
                height={236}
                latestPoint={returnChartLatestPoint}
                onSelectEvent={setActivePage}
              />
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
  )
  const evidence = (
    <div className="home-rail">
        <MarketSummaryPanel activeMarket={activeMarket} summary={marketSummary} />
        <ClosedLoopProofPanel summaries={marketSummaries} />
        <AShareMoneyflowPanel activeMarket={activeMarket} signals={signals} />
        <HomeResultBrief hasHoldingData={hasHoldingData} hasPerformanceData={hasPerformanceData} hasSignalData={hasSignalData} holdings={holdings} portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
        {(activeMarket === 'All Markets' || activeMarket === 'A-share') && (
          <>
            <AShareEvidencePanel evidence={ashareResearchEvidence} forwardValidation={ashareForwardValidation} />
            <AShareTierComparisonPanel activeMarket={activeMarket} summaries={ashareTierSummaries} />
          </>
        )}
    </div>
  )

  return (
    <WorkbenchShell
      active={activeSignals}
      chart={chart}
      completed={completedSignals}
      context={<SignalFunnelFlow events={funnelEvents} hasSignalData={hasSignalData} holdings={holdings} signals={signals} />}
      evidence={evidence}
      liveGate={liveGate}
      onUseSimulation={() => selectAccountMode('simulated')}
      portfolio={portfolio}
      positions={holdings}
      review={reviewItems}
      setActivePage={setActivePage}
    />
  )
}

function getReturnTone(amount: number, pct: number) {
  if (amount < -0.005 || pct < -0.005) {
    return 'negative'
  }
  if (amount > 0.005 || pct > 0.005) {
    return 'positive'
  }
  return 'flat'
}
