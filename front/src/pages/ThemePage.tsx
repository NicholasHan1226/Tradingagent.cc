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
import { SignalDepth } from '../components/panels/SignalDepth'
import { HoldingsTable } from '../components/tables/HoldingsTable'
import { OpportunityTable } from '../components/tables/OpportunityTable'
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
  activePage: Exclude<Page, '主页'>
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
            <PanelTitle kicker="实时收益" title="模拟盘收益走势" />
            <StatusBoundary loading={<ChartSkeleton height={430} />} onRetry={onRetry} status={domainStatus('performance')}>
              <PerformanceChart data={data} events={events} height={430} latestPoint={latestPoint} onSelectEvent={setActivePage} />
            </StatusBoundary>
          </section>
        </section>
        <aside className="theme-rail">
          <ContributionPanel signals={signals} />
          <RiskSnapshot portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
        </aside>
      </div>
    )
  }

  if (activePage === '机会') {
    return (
      <div className="theme-layout single">
        <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="机会" performance={data} portfolio={portfolio} signals={signals} />
        <section className="panel">
          <PanelTitle action="看交易复盘" kicker="当前机会" onAction={() => setActivePage('复盘')} title="当前可处理机会" />
          <StatusBoundary emptyLabel="当前没有需要处理的机会" loading={<TableSkeleton rows={4} />} onRetry={onRetry} status={domainStatus('signals')}>
            <OpportunityTable signals={getActionableSignals(signals)} />
          </StatusBoundary>
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

  if (activePage === '决策') {
    return (
      <div className="theme-layout">
        <section className="theme-main">
          <PageSummaryBoard activeMarket={activeMarket} holdings={holdings} marketSummary={marketSummary} page="决策" performance={data} portfolio={portfolio} signals={signals} />
          <section className="panel">
            <PanelTitle action="看机会" kicker="结果路径" onAction={() => setActivePage('机会')} title="从机会到结果" />
            <StatusBoundary loading={<ChartSkeleton height={300} />} onRetry={onRetry} status={domainStatus('decisions')}>
              <DecisionFormation portfolio={portfolio} signals={signals} />
            </StatusBoundary>
          </section>
        </section>
        <aside className="theme-rail">
          <SignalDepth signals={signals} />
          <RiskSnapshot portfolio={portfolio} setActivePage={setActivePage} signals={signals} />
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
        <PanelTitle kicker="已关闭机会" title="为什么赚，为什么没做" />
        <StatusBoundary emptyLabel="还没有已关闭机会" loading={<TableSkeleton rows={5} />} onRetry={onRetry} status={domainStatus('signals')}>
          <SignalTable signals={getClosedSignals(signals)} />
        </StatusBoundary>
      </section>
    </div>
  )
}
