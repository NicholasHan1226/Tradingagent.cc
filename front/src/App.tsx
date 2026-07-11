import { useEffect, useMemo, useState } from 'react'
import { createTradingAgentSnapshotClient } from './api/tradingAgentIntegration'
import type { TradingAgentReadModelSnapshot } from './api/tradingAgentReadModel'
import { toDashboardState } from './adapters/dashboard'
import { MarketHeader } from './components/MarketHeader'
import { TopNav } from './components/TopNav'
import { MarketTape } from './components/terminal/MarketTape'
import { LinkedEvidenceContext } from './components/terminal/LinkedEvidenceContext'
import { TerminalCommandPalette, type TerminalCommand } from './components/terminal/TerminalCommandPalette'
import { holdings as mockHoldings, markets, mockDashboardApiResponse, pages, performanceData, signals as mockSignals } from './data/dashboard'
import { deriveChartEvents } from './lib/chartEvents'
import { getLivePerformanceData, getSelectedMarketSummary, getVisibleHoldings, getVisibleSignals } from './lib/dashboard'
import { getSnapshotFunnelEvents, getSnapshotHoldings, getSnapshotPerformance, getSnapshotSignals, hasSnapshotRows } from './lib/dashboardSnapshot'
import { createAutomationObservatoryViewModel } from './lib/automationObservatoryViewModel'
import { createWorkbenchViewModel } from './lib/workbenchViewModel'
import { createEvidenceHealth, createMarketPulseHealth, createMarketTapeRows } from './lib/marketTapeViewModel'
import { createRuntimeHeartbeat } from './lib/runtimeHeartbeat'
import { createLinkedEvidenceContext } from './lib/linkedEvidenceContext'
import { readTerminalPreferences, writeTerminalPreferences, type TerminalDensity } from './lib/terminalPreferences'
import { readTerminalNavigation, useTerminalNavigation } from './hooks/useTerminalNavigation'
import { HomeDashboard } from './pages/HomeDashboard'
import { ThemePage } from './pages/ThemePage'
import type { DataDomain } from './types/status'
import type { AccountMode, Market, MarketSummary, Page, PerformancePoint, PerformanceRange, PortfolioSummary } from './types/dashboard'
import './App.css'
import './styles/home-funnel.css'
import './styles/page-summary.css'

const DASHBOARD_BUILD_ID = '20260711-explicit-market-attribution'

function App() {
  const demoPreviewEnabled = isDemoPreviewEnabled()
  const [initialNavigation] = useState(readTerminalNavigation)
  const [activePage, setActivePage] = useState<Page>(initialNavigation.page)
  const [activeMarket, setActiveMarket] = useState<Market>(initialNavigation.market)
  const [performanceRange, setPerformanceRange] = useState<PerformanceRange>(initialNavigation.range)
  const [selectedOpportunityId, setSelectedOpportunityId] = useState<string | null>(initialNavigation.opportunity)
  const [terminalDensity, setTerminalDensity] = useState<TerminalDensity>(() => readTerminalPreferences().density)
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)
  const [accountMode, setAccountMode] = useState<AccountMode>('simulated')
  const [dashboardState, setDashboardState] = useState(() => toDashboardState(mockDashboardApiResponse(demoPreviewEnabled ? 'ready' : 'loading')))
  const [readModelSnapshot, setReadModelSnapshot] = useState<TradingAgentReadModelSnapshot | null>(null)
  const [now, setNow] = useState(() => new Date())
  useTerminalNavigation({ page: activePage, market: activeMarket, range: performanceRange, opportunity: selectedOpportunityId, setPage: setActivePage, setMarket: setActiveMarket, setRange: setPerformanceRange, setOpportunity: setSelectedOpportunityId })

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    const openCommands = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') {
        event.preventDefault()
        setCommandPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', openCommands)
    return () => window.removeEventListener('keydown', openCommands)
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
  const livePerformanceData = useMemo(
    () => getLivePerformanceData(now, performanceRows, isUsingDemoSnapshot),
    [isUsingDemoSnapshot, now, performanceRows],
  )
  const selectedMarketSummary = useMemo(() => getSelectedMarketSummary(marketSummaries, activeMarket), [activeMarket, marketSummaries])
  const visibleSignals = useMemo(() => getVisibleSignals(signalRows, activeMarket), [activeMarket, signalRows])
  const visibleHoldings = useMemo(() => getVisibleHoldings(holdingRows, activeMarket), [activeMarket, holdingRows])
  const marketPerformanceData = useMemo(() => {
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
  const workbench = useMemo(() => createWorkbenchViewModel({
    accountMode,
    activeMarket,
    performance: marketPerformanceData,
    portfolio: portfolioSummary,
    marketSummaries,
    signals: signalRows,
    holdings: holdingRows,
    funnelEvents,
    generatedAt: readModelSnapshot?.generatedAt ?? null,
  }), [accountMode, activeMarket, funnelEvents, holdingRows, marketPerformanceData, marketSummaries, portfolioSummary, readModelSnapshot?.generatedAt, signalRows])
  const observatory = useMemo(() => createAutomationObservatoryViewModel(workbench), [workbench])
  const visiblePerformanceData = workbench.performance
  const visiblePortfolio = workbench.portfolio
  const latestPoint = visiblePerformanceData[visiblePerformanceData.length - 1] ?? {
    day: '现在',
    simulated: 0,
    target: 0,
    benchmark: 0,
    opportunity: 0,
  }
  const visibleFunnelEvents = workbench.funnelEvents
  const chartEvents = useMemo(() => deriveChartEvents(visiblePerformanceData, visibleSignals), [visiblePerformanceData, visibleSignals])
  const domainStatus = (domain: DataDomain) => dashboardState.domains[domain]?.status ?? dashboardState.status
  const handleRetry = () => setDashboardState(toDashboardState(mockDashboardApiResponse(demoPreviewEnabled ? 'ready' : 'loading')))
  const selectAccountMode = (mode: AccountMode) => setAccountMode(mode)
  const marketTapeRows = useMemo(() => createMarketTapeRows(marketSummaries, activeMarket, readModelSnapshot?.generatedAt ?? null, readModelSnapshot?.marketPulses ?? []), [activeMarket, marketSummaries, readModelSnapshot?.generatedAt, readModelSnapshot?.marketPulses])
  const marketPulseHealth = useMemo(() => createMarketPulseHealth(readModelSnapshot?.marketPulseCoverage, readModelSnapshot?.marketPulseCoverageHistory), [readModelSnapshot?.marketPulseCoverage, readModelSnapshot?.marketPulseCoverageHistory])
  const evidenceHealth = useMemo(() => createEvidenceHealth(dashboardState.domains, readModelSnapshot?.generatedAt ?? null, selectedMarketSummary), [dashboardState.domains, readModelSnapshot?.generatedAt, selectedMarketSummary])
  const heartbeat = useMemo(() => createRuntimeHeartbeat({
    domains: dashboardState.domains,
    funnelEvents: visibleFunnelEvents,
    generatedAt: isUsingDemoSnapshot ? now.toISOString() : readModelSnapshot?.generatedAt ?? null,
    marketSummary: selectedMarketSummary,
    now,
    signals: visibleSignals,
  }), [dashboardState.domains, isUsingDemoSnapshot, now, readModelSnapshot?.generatedAt, selectedMarketSummary, visibleFunnelEvents, visibleSignals])
  const linkedContext = useMemo(() => createLinkedEvidenceContext(funnelEvents, selectedOpportunityId, signalRows, holdingRows), [funnelEvents, holdingRows, selectedOpportunityId, signalRows])
  const terminalCommands = useMemo<TerminalCommand[]>(() => [
    ...pages.map((page, index) => ({ id: `page:${page}`, label: `打开${page}终端`, group: '页面', hint: `Alt+${index + 1}`, keywords: page })),
    ...markets.map((market) => ({ id: `market:${market}`, label: `切换到${market === 'All Markets' ? '全市场' : market}`, group: '市场', keywords: market })),
    { id: 'density', label: terminalDensity === 'compact' ? '切换舒适密度' : '切换紧凑密度', group: '视图', hint: '本地', keywords: 'density compact comfortable 密度' },
    ...(linkedContext ? [{ id: 'clear-context', label: '清除关联机会', group: '上下文', hint: linkedContext.symbol, keywords: linkedContext.id }] : []),
  ], [linkedContext, terminalDensity])

  const executeTerminalCommand = (id: string) => {
    if (id.startsWith('page:')) setActivePage(id.slice(5) as Page)
    else if (id.startsWith('market:')) setActiveMarket(id.slice(7) as Market)
    else if (id === 'clear-context') setSelectedOpportunityId(null)
    else if (id === 'density') {
      const density: TerminalDensity = terminalDensity === 'compact' ? 'comfortable' : 'compact'
      setTerminalDensity(density)
      const current = readTerminalPreferences()
      writeTerminalPreferences({ ...current, density })
    }
  }

  useEffect(() => {
    if (selectedOpportunityId && readModelSnapshot && !linkedContext) setSelectedOpportunityId(null)
  }, [linkedContext, readModelSnapshot, selectedOpportunityId])

  return (
    <main className={`hyper-shell${linkedContext ? ' has-linked-context' : ''}`} data-build={DASHBOARD_BUILD_ID} data-density={terminalDensity}>
      <TopNav
        activePage={activePage}
        heartbeat={heartbeat}
        setActivePage={setActivePage}
        onOpenCommandPalette={() => setCommandPaletteOpen(true)}
      />
      <MarketHeader
        accountMode={accountMode}
        completedCount={observatory.summary.completedCount}
        activePage={activePage}
        activeMarket={activeMarket}
        hasPerformanceData={hasPerformanceData}
        isDemoPreview={isUsingDemoSnapshot}
        isCnyAccount={visiblePortfolio?.pnlCurrency === 'CNY'}
        liveProfit={visiblePortfolio?.pnlAmount ?? null}
        liveReturn={visiblePortfolio?.returnPct ?? latestPoint.simulated}
        maxDrawdown={visiblePortfolio?.maxDrawdownPct ?? (visiblePerformanceData.length ? getPerformanceDrawdown(visiblePerformanceData) : null)}
        positionCount={visibleHoldings.length}
        performanceStatus={domainStatus('performance')}
        runningCount={observatory.summary.runningCount}
        heartbeat={heartbeat}
        snapshotGeneratedAt={readModelSnapshot?.generatedAt ?? null}
        setActiveMarket={setActiveMarket}
        targetReturn={visiblePortfolio?.targetPct ?? latestPoint.target}
      />
      <MarketTape evidence={evidenceHealth} onSelect={setActiveMarket} pulseHealth={marketPulseHealth} rows={marketTapeRows} />
      {linkedContext && <LinkedEvidenceContext model={linkedContext} onClear={() => setSelectedOpportunityId(null)} onOpenProcess={() => setActivePage('过程')} />}

      <section className="workspace">
        {activePage === '总览' || workbench.liveGate.gated ? (
          <HomeDashboard
            accountMode={accountMode}
            activeSignals={observatory.running}
            activeMarket={activeMarket}
            ashareForwardValidation={readModelSnapshot?.ashareForwardValidation}
            ashareResearchEvidence={readModelSnapshot?.ashareResearchEvidence}
            ashareTierSummaries={readModelSnapshot?.ashareTierSummaries}
            data={visiblePerformanceData}
            hasPerformanceData={hasPerformanceData}
            hasSignalData={hasSignalData}
            holdings={visibleHoldings}
            latestPoint={latestPoint}
            liveGate={workbench.liveGate}
            marketSummary={selectedMarketSummary}
            marketSummaries={marketSummaries}
            now={now}
            portfolio={visiblePortfolio}
            completedSignals={observatory.completed}
            domainStatus={domainStatus}
            onRetry={handleRetry}
            selectAccountMode={selectAccountMode}
            setActivePage={setActivePage}
            signals={visibleSignals}
            reviewItems={observatory.automaticReview}
            runningCount={observatory.summary.runningCount}
            runtimeItem={observatory.runtimeItem}
            snapshotGeneratedAt={readModelSnapshot?.generatedAt ?? null}
            funnelEvents={visibleFunnelEvents}
            events={chartEvents}
          />
        ) : (
          <ThemePage
            activePage={activePage}
            activeMarket={activeMarket}
            data={visiblePerformanceData}
            latestPoint={latestPoint}
            performanceRange={performanceRange}
            holdings={visibleHoldings}
            marketSummary={selectedMarketSummary}
            portfolio={visiblePortfolio}
            domainStatus={domainStatus}
            onRetry={handleRetry}
            setActivePage={setActivePage}
            setPerformanceRange={setPerformanceRange}
            signals={visibleSignals}
            events={chartEvents}
            funnelEvents={visibleFunnelEvents}
            heartbeat={heartbeat}
            selectedOpportunityId={selectedOpportunityId}
            setSelectedOpportunityId={setSelectedOpportunityId}
            snapshotGeneratedAt={readModelSnapshot?.generatedAt ?? null}
          />
        )}
      </section>
      {commandPaletteOpen && <TerminalCommandPalette commands={terminalCommands} onClose={() => setCommandPaletteOpen(false)} onExecute={executeTerminalCommand} />}
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

function getPerformanceDrawdown(rows: PerformancePoint[]) {
  if (!rows.length) return 0
  let peak = rows[0]?.simulated ?? 0
  return rows.reduce((maximum, point) => {
    peak = Math.max(peak, point.simulated)
    return Math.max(maximum, peak - point.simulated)
  }, 0)
}

function isDemoPreviewEnabled() {
  const configuredPreview = import.meta.env.VITE_TRADING_AGENT_DEMO_PREVIEW
  if (configuredPreview === '0') return false
  return configuredPreview === '1' || import.meta.env.DEV
}
