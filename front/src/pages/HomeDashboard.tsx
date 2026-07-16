import { PerformanceChart } from '../components/charts/PerformanceChart'
import { ChartSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { AShareEvidencePanel } from '../components/panels/AShareEvidencePanel'
import { AShareMoneyflowPanel } from '../components/panels/AShareMoneyflowPanel'
import { AShareTierComparisonPanel } from '../components/panels/AShareTierComparisonPanel'
import { ClosedLoopProofPanel } from '../components/panels/ClosedLoopProofPanel'
import { MarketSummaryPanel } from '../components/panels/MarketSummaryPanel'
import { MarketMaturityPanel } from '../components/panels/MarketMaturityPanel'
import { RealtimeReturnCard } from '../components/panels/RealtimeReturnCard'
import { SignalFunnelFlow } from '../components/panels/SignalFunnelFlow'
import { TodayRunPanel } from '../components/panels/TodayRunPanel'
import { WorkbenchShell } from '../components/workbench/WorkbenchShell'
import { formatTime } from '../lib/format'
import { getSignalFunnel } from '../lib/dashboard'
import type { AutomationRuntimeItem } from '../lib/automationObservatoryViewModel'
import type { AShareForwardValidation, AShareMarketMaturityProjection, AShareResearchEvidence, AShareSampleKpiProjection, AShareTierSummary, AccountMode, ChartEvent, CNFuturesMarketMaturityProjection, FunnelEvent, HoldingRow, Market, MarketSummary, Page, PaperDayRunSummary, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function HomeDashboard({
  accountMode,
  ashareForwardValidation,
  ashareMarketMaturity,
  activeMarket,
  ashareResearchEvidence,
  ashareSampleKpi,
  ashareTierSummaries,
  cnFuturesMarketMaturity,
  data,
  latestPoint,
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
  snapshotGeneratedAt,
  runningCount,
  runtimeItem,
  paperDayRun,
}: {
  accountMode: AccountMode
  activeMarket: Market
  ashareForwardValidation?: AShareForwardValidation
  ashareMarketMaturity?: AShareMarketMaturityProjection
  ashareResearchEvidence?: AShareResearchEvidence
  ashareSampleKpi?: AShareSampleKpiProjection
  ashareTierSummaries?: AShareTierSummary[]
  cnFuturesMarketMaturity?: CNFuturesMarketMaturityProjection
  data: PerformancePoint[]
  events: ChartEvent[]
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
  snapshotGeneratedAt: string | null
  runningCount: number
  runtimeItem: AutomationRuntimeItem
  paperDayRun?: PaperDayRunSummary
}) {
  const signalFunnel = getSignalFunnel(signals)
  const liveProfit = portfolio?.pnlAmount ?? 0
  const liveReturn = portfolio?.returnPct ?? latestPoint.simulated
  const targetReturn = portfolio?.targetPct ?? latestPoint.target
  const targetGap = liveReturn - targetReturn
  const returnTone = getReturnTone(liveProfit, liveReturn)
  const returnChartLatestPoint = data[data.length - 1] ?? latestPoint
  const performanceStatus = domainStatus('performance')
  const snapshotTime = getSnapshotTime(snapshotGeneratedAt, now)
  const headline = hasPerformanceData
    ? targetGap >= 0
      ? '当前收益领先目标，回撤仍在边界内。'
      : '收益暂时落后目标，自动流程继续校准机会质量与风险距离。'
    : '收益结果尚未写入，自动流程会持续运行并回写结果。'

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
              targetReturn={targetReturn}
            />
          </div>
          <div className="chart-section-title">
            <span>收益曲线</span>
            <strong>{hasPerformanceData ? '持续性与风险距离' : '等待收益、目标和基准数据'}</strong>
          </div>
          <StatusBoundary loading={<ChartSkeleton height={220} />} onRetry={onRetry} status={hasPerformanceData ? performanceStatus : 'ready'}>
            {hasPerformanceData ? (
              <PerformanceChart
                currentTone={returnTone}
                data={data}
                events={events}
                height={220}
                latestPoint={returnChartLatestPoint}
                onSelectEvent={setActivePage}
              />
            ) : (
              <div className="chart-empty-state" style={{ height: 220 }}>
                <span>等待收益序列</span>
                <strong>连接正常，暂无可展示的收益曲线。</strong>
                <p>当模拟盘写入净值、目标和市场基准后，这里会自动更新。</p>
              </div>
            )}
          </StatusBoundary>
          <div className="chart-meta">
            <span>{formatTime(snapshotTime)} (UTC+8)</span>
            <b>{hasPerformanceData ? performanceStatus === 'stale' ? '数据滞后' : '快照时间' : '等待数据'}</b>
            <em>{hasPerformanceData ? `机会差 ${returnChartLatestPoint.opportunity.toFixed(2)}%` : '未显示样例收益'}</em>
          </div>
    </section>
  )
  const evidence = (
    <div className="home-rail">
        <TodayRunPanel run={paperDayRun} />
        <MarketSummaryPanel activeMarket={activeMarket} summary={marketSummary} />
        <MarketMaturityPanel
          activeMarket={activeMarket}
          ashareMaturity={ashareMarketMaturity}
          ashareSampleKpi={ashareSampleKpi}
          cnFuturesMaturity={cnFuturesMarketMaturity}
          marketSummaries={marketSummaries}
        />
        <ClosedLoopProofPanel summaries={marketSummaries} />
        <AShareMoneyflowPanel activeMarket={activeMarket} signals={signals} />
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
      context={<SignalFunnelFlow compact events={funnelEvents} hasSignalData={hasSignalData} holdings={holdings} signals={signals} />}
      evidence={evidence}
      liveGate={liveGate}
      onUseSimulation={() => selectAccountMode('simulated')}
      positions={holdings}
      review={reviewItems}
      runningCount={runningCount}
      runtimeItem={runtimeItem}
    />
  )
}

function getSnapshotTime(value: string | null, fallback: Date) {
  if (!value) return fallback
  const timestamp = new Date(value)
  return Number.isNaN(timestamp.getTime()) ? fallback : timestamp
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
