import { AllocationPanel } from '../components/charts/AllocationPanel'
import { ContributionPanel } from '../components/charts/ContributionPanel'
import { PerformanceChart } from '../components/charts/PerformanceChart'
import { RiskTimeline } from '../components/charts/RiskTimeline'
import { ChartSkeleton, TableSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { DecisionFormation } from '../components/panels/DecisionFormation'
import { OpportunityFocus } from '../components/panels/OpportunityFocus'
import { ResultSummary } from '../components/panels/ResultSummary'
import { RiskSnapshot } from '../components/panels/RiskSnapshot'
import { HoldingsTable } from '../components/tables/HoldingsTable'
import { RunningProcessTable } from '../components/tables/RunningProcessTable'
import { SignalTable } from '../components/tables/SignalTable'
import { PanelTitle } from '../components/PanelTitle'
import { PageSummaryBoard } from '../components/PageSummaryBoard'
import { getActionableSignals, getClosedSignals } from '../lib/dashboard'
import type { ChartEvent, HoldingRow, Market, MarketSummary, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function ThemePage({
  activePage,
  activeMarket,
  data,
  latestPoint,
  holdings,
  marketSummary,
  portfolio,
  domainStatus,
  onRetry,
  setActivePage,
  signals,
  events,
}: {
  activePage: Exclude<Page, '总览'>
  activeMarket: Market
  data: PerformancePoint[]
  events: ChartEvent[]
  holdings: HoldingRow[]
  latestPoint: PerformancePoint
  marketSummary?: MarketSummary
  portfolio: PortfolioSummary | null
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  if (activePage === '收益') {
    return (
      <div className="theme-layout">
        <section className="theme-main">
          <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="收益" performance={data} portfolio={portfolio} signals={signals} />
          <section className="panel tall-panel">
            <PanelTitle kicker="收益结果" title="模拟盘收益走势" />
            <StatusBoundary loading={<ChartSkeleton height={430} />} onRetry={onRetry} status={domainStatus('performance')}>
              <PerformanceChart
                currentTone={getPerformanceTone(latestPoint.simulated)}
                data={data}
                events={events}
                height={430}
                latestPoint={latestPoint}
                onSelectEvent={setActivePage}
                showRangeControls
              />
            </StatusBoundary>
          </section>
        </section>
        <aside className="theme-rail">
          <ContributionPanel signals={signals} />
        </aside>
      </div>
    )
  }

  if (activePage === '过程') {
    return (
      <div className="theme-layout single">
        <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="过程" performance={data} portfolio={portfolio} signals={signals} />
        <section className="panel">
          <PanelTitle kicker="运行阶段" title="自动化过程" />
          <StatusBoundary emptyLabel="当前没有运行中的自动过程" loading={<TableSkeleton rows={4} />} onRetry={onRetry} status={domainStatus('signals')}>
            <RunningProcessTable signals={getActionableSignals(signals)} />
          </StatusBoundary>
        </section>
        <section className="panel">
          <PanelTitle kicker="过程结果" title="从发现到结果写回" />
          <DecisionFormation portfolio={portfolio} signals={signals} />
        </section>
      </div>
    )
  }

  if (activePage === '持仓') {
    return (
      <div className="theme-layout">
        <section className="theme-main">
          <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="持仓" performance={data} portfolio={portfolio} signals={signals} />
          <section className="panel">
            <PanelTitle kicker="持仓贡献" title="当前持仓结果" />
            <StatusBoundary loading={<TableSkeleton rows={4} />} onRetry={onRetry} status={domainStatus('holdings')}>
              <HoldingsTable holdings={holdings} />
            </StatusBoundary>
          </section>
        </section>
        <aside className="theme-rail">
          <AllocationPanel holdings={holdings} />
          <ResultSummary holdings={holdings} portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
        </aside>
      </div>
    )
  }

  if (activePage === '风险') {
    return (
      <div className="theme-layout">
        <section className="theme-main">
          <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="风险" performance={data} portfolio={portfolio} signals={signals} />
          <section className="panel">
            <PanelTitle kicker="风险变化" title="回撤与保护结果" />
            <StatusBoundary loading={<ChartSkeleton height={320} />} onRetry={onRetry} status={domainStatus('risk')}>
              <RiskTimeline data={data} portfolio={portfolio} />
            </StatusBoundary>
          </section>
        </section>
        <aside className="theme-rail">
          <RiskSnapshot portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
          <OpportunityFocus setActivePage={setActivePage} signals={signals} />
        </aside>
      </div>
    )
  }

  return (
    <div className="theme-layout single">
      <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="复盘" performance={data} portfolio={portfolio} signals={signals} />
      <section className="panel">
        <PanelTitle kicker="已关闭过程" title="自动复盘归因" />
        <StatusBoundary emptyLabel="还没有已关闭机会" loading={<TableSkeleton rows={5} />} onRetry={onRetry} status={domainStatus('signals')}>
          <SignalTable signals={getClosedSignals(signals)} />
        </StatusBoundary>
      </section>
    </div>
  )
}

function getPerformanceTone(value: number) {
  if (value < -0.005) return 'negative' as const
  if (value > 0.005) return 'positive' as const
  return 'flat' as const
}
