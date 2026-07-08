import { useEffect, useMemo, useState } from 'react'
import { createTradingAgentSnapshotClient } from './api/tradingAgentIntegration'
import type { TradingAgentReadModelSnapshot } from './api/tradingAgentReadModel'
import { toDashboardState } from './adapters/dashboard'
import { MarketHeader } from './components/MarketHeader'
import { TopNav } from './components/TopNav'
import { holdings as mockHoldings, mockDashboardApiResponse, performanceData, signals as mockSignals } from './data/dashboard'
import { deriveChartEvents } from './lib/chartEvents'
import { getLivePerformanceData, getPortfolioForView, getSelectedMarketSummary, getSignalFunnel, getVisibleHoldings, getVisibleSignals } from './lib/dashboard'
import { getSnapshotFunnelEvents, getSnapshotHoldings, getSnapshotPerformance, getSnapshotSignals, hasSnapshotRows } from './lib/dashboardSnapshot'
import { HomeDashboard } from './pages/HomeDashboard'
import { ThemePage } from './pages/ThemePage'
import type { DataDomain } from './types/status'
import type { AccountMode, Market, MarketSummary, Page, PerformancePoint, PortfolioSummary } from './types/dashboard'
import './App.css'
import './styles/home-funnel.css'
import './styles/page-summary.css'

const DASHBOARD_BUILD_ID = '20260708-result-funnel'

function App() {
  const demoPreviewEnabled = isDemoPreviewEnabled()
  const [activePage, setActivePage] = useState<Page>('主页')
  const [activeMarket, setActiveMarket] = useState<Market>('All Markets')
  const [accountMode, setAccountMode] = useState<AccountMode>('simulated')
  const [dashboardState, setDashboardState] = useState(() => toDashboardState(mockDashboardApiResponse(demoPreviewEnabled ? 'ready' : 'loading')))
  const [readModelSnapshot, setReadModelSnapshot] = useState<TradingAgentReadModelSnapshot | null>(null)
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    const client = createTradingAgentSnapshotClient({ timeoutMs: 4000 })
    let mounted = true

    async function refreshSnapshot() {
      try {
        const snapshot = await client.getSnapshot()
        if (mounted) {
          setReadModelSnapshot(snapshot)
          setDashboardState({
            mode: snapshot.mode,
            status: 'ready',
            domains: snapshot.domains,
          })
        }
      } catch {
        if (mounted) {
          setReadModelSnapshot(null)
          if (!demoPreviewEnabled) setDashboardState(toDashboardState(mockDashboardApiResponse('error')))
        }
      }
    }

    void refreshSnapshot()
    const timer = window.setInterval(() => void refreshSnapshot(), 5000)
    return () => {
      mounted = false
      window.clearInterval(timer)
    }
  }, [demoPreviewEnabled])

  const performanceRows = useMemo(() => getSnapshotPerformance(readModelSnapshot, demoPreviewEnabled ? performanceData : []), [demoPreviewEnabled, readModelSnapshot])
  const signalRows = useMemo(() => getSnapshotSignals(readModelSnapshot, demoPreviewEnabled ? mockSignals : []), [demoPreviewEnabled, readModelSnapshot])
  const holdingRows = useMemo(() => getSnapshotHoldings(readModelSnapshot, demoPreviewEnabled ? mockHoldings : []), [demoPreviewEnabled, readModelSnapshot])
  const funnelEvents = useMemo(() => getSnapshotFunnelEvents(readModelSnapshot, []), [readModelSnapshot])
  const marketSummaries = useMemo(() => readModelSnapshot?.marketSummaries ?? [], [readModelSnapshot])
  const portfolioSummary = readModelSnapshot?.portfolio ?? null
  const isUsingDemoSnapshot = readModelSnapshot === null && demoPreviewEnabled
  const hasGlobalPerformanceData = isUsingDemoSnapshot || hasMeaningfulPerformanceRows(readModelSnapshot?.performance ?? []) || hasPortfolioResult(portfolioSummary)
  const hasGlobalSignalData = isUsingDemoSnapshot || hasSnapshotRows(readModelSnapshot, 'signals')
  const hasGlobalHoldingData = isUsingDemoSnapshot || hasSnapshotRows(readModelSnapshot, 'holdings')
  const livePerformanceData = useMemo(
    () => getLivePerformanceData(now, performanceRows, isUsingDemoSnapshot),
    [isUsingDemoSnapshot, now, performanceRows],
  )
  const selectedMarketSummary = useMemo(() => getSelectedMarketSummary(marketSummaries, activeMarket), [activeMarket, marketSummaries])
  const visibleSignals = useMemo(() => getVisibleSignals(signalRows, activeMarket), [activeMarket, signalRows])
  const visibleHoldings = useMemo(() => getVisibleHoldings(holdingRows, activeMarket), [activeMarket, holdingRows])
  const visiblePerformanceData = useMemo(() => {
    if (activeMarket === 'All Markets') return livePerformanceData
    if (selectedMarketSummary?.returnPct === undefined) return []
    return [{
      day: '现在',
      simulated: selectedMarketSummary.returnPct,
      target: portfolioSummary?.targetPct ?? 8,
      benchmark: 0,
      opportunity: selectedMarketSummary.maxDrawdownPct === undefined ? 0 : -Math.abs(selectedMarketSummary.maxDrawdownPct),
    }]
  }, [activeMarket, livePerformanceData, portfolioSummary?.targetPct, selectedMarketSummary])
  const hasPerformanceData = activeMarket === 'All Markets'
    ? hasGlobalPerformanceData
    : hasMarketPerformanceResult(selectedMarketSummary)
  const hasSignalData = activeMarket === 'All Markets' ? hasGlobalSignalData : visibleSignals.length > 0
  const hasHoldingData = activeMarket === 'All Markets' ? hasGlobalHoldingData : visibleHoldings.length > 0
  const visiblePortfolio = useMemo(
    () => getPortfolioForView({ activeMarket, marketSummaries, portfolio: portfolioSummary }),
    [activeMarket, marketSummaries, portfolioSummary],
  )
  const latestPoint = visiblePerformanceData[visiblePerformanceData.length - 1] ?? {
    day: '现在',
    simulated: 0,
    target: 0,
    benchmark: 0,
    opportunity: 0,
  }
  const visibleFunnelEvents = useMemo(
    () => funnelEvents.filter((event) => activeMarket === 'All Markets' || event.market === activeMarket),
    [activeMarket, funnelEvents],
  )
  const signalFunnel = useMemo(() => getSignalFunnel(visibleSignals), [visibleSignals])
  const chartEvents = useMemo(() => deriveChartEvents(visiblePerformanceData, visibleSignals), [visiblePerformanceData, visibleSignals])
  const domainStatus = (domain: DataDomain) => dashboardState.domains[domain]?.status ?? dashboardState.status
  const handleRetry = () => setDashboardState(toDashboardState(mockDashboardApiResponse(demoPreviewEnabled ? 'ready' : 'loading')))
  const selectAccountMode = (mode: AccountMode) => setAccountMode(mode)

  return (
    <main className="hyper-shell" data-build={DASHBOARD_BUILD_ID}>
      <TopNav
        activePage={activePage}
        setActivePage={setActivePage}
      />
      <MarketHeader
        accountMode={accountMode}
        activePage={activePage}
        activeMarket={activeMarket}
        hasPerformanceData={hasPerformanceData}
        isDemoPreview={isUsingDemoSnapshot}
        isCnyAccount={visiblePortfolio?.pnlCurrency === 'CNY'}
        liveProfit={visiblePortfolio?.pnlAmount ?? null}
        liveReturn={visiblePortfolio?.returnPct ?? latestPoint.simulated}
        maxDrawdown={visiblePortfolio?.maxDrawdownPct ?? null}
        signalCount={visibleSignals.length}
        snapshotGeneratedAt={readModelSnapshot?.generatedAt ?? null}
        setActiveMarket={setActiveMarket}
        targetReturn={visiblePortfolio?.targetPct ?? latestPoint.target}
        tradeSignalCount={signalFunnel.tradeSignals.length}
      />

      <section className="workspace">
        {activePage === '主页' ? (
          <HomeDashboard
            accountMode={accountMode}
            activeMarket={activeMarket}
            ashareResearchEvidence={readModelSnapshot?.ashareResearchEvidence}
            data={visiblePerformanceData}
            hasHoldingData={hasHoldingData}
            hasPerformanceData={hasPerformanceData}
            hasSignalData={hasSignalData}
            holdings={visibleHoldings}
            latestPoint={latestPoint}
            marketSummary={selectedMarketSummary}
            marketSummaries={marketSummaries}
            now={now}
            portfolio={visiblePortfolio}
            domainStatus={domainStatus}
            onRetry={handleRetry}
            selectAccountMode={selectAccountMode}
            setActivePage={setActivePage}
            signals={visibleSignals}
            funnelEvents={visibleFunnelEvents}
            events={chartEvents}
          />
        ) : (
          <ThemePage
            activePage={activePage}
            activeMarket={activeMarket}
            data={visiblePerformanceData}
            latestPoint={latestPoint}
            holdings={visibleHoldings}
            marketSummary={selectedMarketSummary}
            portfolio={visiblePortfolio}
            domainStatus={domainStatus}
            onRetry={handleRetry}
            setActivePage={setActivePage}
            signals={visibleSignals}
            events={chartEvents}
          />
        )}
      </section>
    </main>
  )
}

export default App

function hasMeaningfulPerformanceRows(rows: PerformancePoint[]) {
  if (rows.length > 1) return true
  return rows.some((point) =>
    Math.abs(point.simulated) > 0.005 ||
    Math.abs(point.benchmark) > 0.005 ||
    Math.abs(point.opportunity) > 0.005
  )
}

function hasPortfolioResult(portfolio: PortfolioSummary | null) {
  if (!portfolio) return false
  if (portfolio.tradeCount > 0 || portfolio.pointCount > 1) return true
  if (Math.abs(portfolio.pnlAmount) > 0.005 || Math.abs(portfolio.returnPct) > 0.005) return true
  const account = portfolio.ashareAccount
  if (!account) return false
  return account.openPositionCount > 0 || account.totalSampleCount > 0 || Math.abs(account.accountTotalPnl) > 0.005
}

function hasMarketPerformanceResult(summary?: MarketSummary) {
  if (!summary || summary.status === 'empty') return false
  if (summary.tradeCount > 0 || (summary.filledCount ?? 0) > 0) return true
  return Math.abs(summary.pnlAmount ?? 0) > 0.005 || Math.abs(summary.returnPct ?? 0) > 0.005
}

function isDemoPreviewEnabled() {
  const meta = import.meta as ImportMeta & { env?: { DEV?: boolean; VITE_TRADING_AGENT_DEMO_PREVIEW?: string } }
  if (meta.env?.VITE_TRADING_AGENT_DEMO_PREVIEW === '0') return false
  return meta.env?.VITE_TRADING_AGENT_DEMO_PREVIEW === '1' || meta.env?.DEV === true
}
