import { useEffect, useMemo, useState } from 'react'
import { createTradingAgentSnapshotClient } from './api/tradingAgentIntegration'
import type { TradingAgentReadModelSnapshot } from './api/tradingAgentReadModel'
import { toDashboardState } from './adapters/dashboard'
import { MarketHeader } from './components/MarketHeader'
import { TopNav } from './components/TopNav'
import { holdings as mockHoldings, mockDashboardApiResponse, performanceData, signals as mockSignals } from './data/dashboard'
import { deriveChartEvents } from './lib/chartEvents'
import { getLivePerformanceData, getSignalFunnel, getVisibleSignals } from './lib/dashboard'
import { getSnapshotHoldings, getSnapshotPerformance, getSnapshotSignals, hasSnapshotRows } from './lib/dashboardSnapshot'
import { HomeDashboard } from './pages/HomeDashboard'
import { ThemePage } from './pages/ThemePage'
import type { DataDomain } from './types/status'
import type { AccountMode, Market, Page } from './types/dashboard'
import './App.css'

function App() {
  const [activePage, setActivePage] = useState<Page>('主页')
  const [activeMarket, setActiveMarket] = useState<Market>('All Markets')
  const [accountMode, setAccountMode] = useState<AccountMode>('simulated')
  const [dashboardState, setDashboardState] = useState(() => toDashboardState(mockDashboardApiResponse('ready')))
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
        if (mounted) setReadModelSnapshot(null)
      }
    }

    void refreshSnapshot()
    const timer = window.setInterval(() => void refreshSnapshot(), 5000)
    return () => {
      mounted = false
      window.clearInterval(timer)
    }
  }, [])

  const performanceRows = useMemo(() => getSnapshotPerformance(readModelSnapshot, performanceData), [readModelSnapshot])
  const signalRows = useMemo(() => getSnapshotSignals(readModelSnapshot, mockSignals), [readModelSnapshot])
  const holdingRows = useMemo(() => getSnapshotHoldings(readModelSnapshot, mockHoldings), [readModelSnapshot])
  const portfolioSummary = readModelSnapshot?.portfolio ?? null
  const isUsingDemoSnapshot = readModelSnapshot === null
  const hasPerformanceData = isUsingDemoSnapshot || hasSnapshotRows(readModelSnapshot, 'performance') || Boolean(portfolioSummary)
  const hasSignalData = isUsingDemoSnapshot || hasSnapshotRows(readModelSnapshot, 'signals')
  const hasHoldingData = isUsingDemoSnapshot || hasSnapshotRows(readModelSnapshot, 'holdings')
  const livePerformanceData = useMemo(
    () => getLivePerformanceData(now, performanceRows, isUsingDemoSnapshot),
    [isUsingDemoSnapshot, now, performanceRows],
  )
  const latestPoint = livePerformanceData[livePerformanceData.length - 1] ?? {
    day: '现在',
    simulated: 0,
    target: 0,
    benchmark: 0,
    opportunity: 0,
  }
  const visibleSignals = useMemo(() => getVisibleSignals(signalRows, activeMarket), [activeMarket, signalRows])
  const signalFunnel = useMemo(() => getSignalFunnel(visibleSignals), [visibleSignals])
  const chartEvents = useMemo(() => deriveChartEvents(livePerformanceData, visibleSignals), [livePerformanceData, visibleSignals])
  const domainStatus = (domain: DataDomain) => dashboardState.domains[domain]?.status ?? dashboardState.status
  const handleRetry = () => setDashboardState(toDashboardState(mockDashboardApiResponse('ready')))
  const selectAccountMode = (mode: AccountMode) => setAccountMode(mode)

  return (
    <main className="hyper-shell">
      <TopNav
        activePage={activePage}
        setActivePage={setActivePage}
      />
      <MarketHeader
        activePage={activePage}
        activeMarket={activeMarket}
        hasPerformanceData={hasPerformanceData}
        liveProfit={portfolioSummary?.pnlAmount ?? null}
        liveReturn={portfolioSummary?.returnPct ?? latestPoint.simulated}
        maxDrawdown={portfolioSummary?.maxDrawdownPct ?? null}
        signalCount={visibleSignals.length}
        snapshotGeneratedAt={readModelSnapshot?.generatedAt ?? null}
        setActiveMarket={setActiveMarket}
        targetReturn={portfolioSummary?.targetPct ?? latestPoint.target}
        tradeSignalCount={signalFunnel.tradeSignals.length}
      />

      <section className="workspace">
        {activePage === '主页' ? (
          <HomeDashboard
            accountMode={accountMode}
            data={livePerformanceData}
            hasHoldingData={hasHoldingData}
            hasPerformanceData={hasPerformanceData}
            hasSignalData={hasSignalData}
            holdings={holdingRows}
            latestPoint={latestPoint}
            now={now}
            portfolio={portfolioSummary}
            domainStatus={domainStatus}
            onRetry={handleRetry}
            selectAccountMode={selectAccountMode}
            setActivePage={setActivePage}
            signals={visibleSignals}
            events={chartEvents}
          />
        ) : (
          <ThemePage
            activePage={activePage}
            data={livePerformanceData}
            latestPoint={latestPoint}
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
