import { access, readFile, readdir } from 'node:fs/promises'
import { basename, join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel.ts'
import type { ApiStatus } from '../api/types.ts'
import type { AShareResearchEvidence, FunnelEvent, FunnelEventStatus, HoldingRow, Market, MarketSummary, PerformancePoint, PortfolioSummary, SignalRow, SignalStatus } from '../types/dashboard.ts'

type SnapshotOptions = {
  workspaceRoot: string
  signalQueueDir?: string
  now?: Date
}

type PositionRow = {
  ts_code?: string
  quantity?: number
  sellable_quantity?: number
  avg_price?: number
  cost_basis?: number
  market_value?: number
  side?: string
  price?: number
  pnl?: number
  running_avg_price?: number
  running_cost?: number
  realized_pnl?: number
  unrealized_pnl?: number
  entry_date?: string
  thesis?: string
}

type PositionPlanFile = {
  positions?: PositionRow[]
}

type CNFuturesPositionRow = {
  symbol?: string
  style?: string
  net_qty?: number
  avg_price?: number
  mark_price?: number
  margin_required?: number
  realized_pnl?: number
  unrealized_pnl?: number
}

type CNFuturesPositionsFile = {
  positions?: CNFuturesPositionRow[]
}

type SimLedgerPosition = {
  avg_cost?: number
  quantity?: number
  realized_pnl?: number
}

type SimLedgerPositionsFile = {
  cash?: number
  positions?: Record<string, SimLedgerPosition>
}

type LocalSimTradeRow = {
  candidate_pool_layer?: string
  execution_source?: string
  market?: string
  side?: string
  status?: string
}

type LocalSimAccountPnl = {
  cash_available?: number | string
  market_value?: number | string
  total_pnl?: number | string
  positions?: Record<string, unknown>
}

type SignalFile = {
  ts_code?: string
  symbol?: string
  market?: string
  direction?: string
  status?: string
  confidence?: string | number
  reason?: string
  expected_alpha_bps?: number
  alpha_bps?: number
  timestamp?: string
  discovered_at?: string
  scored_at?: string
  debated_at?: string
  risk_checked_at?: string
  triggered_at?: string
  created_at?: string
  updated_at?: string
  risk_check?: {
    passed?: boolean
    checks?: string[]
  }
  trigger?: {
    triggered_at?: string
    trigger_price?: number | null
  }
  fill?: {
    filled_at?: string
    filled_price?: number
    filled_qty?: number
  }
  simulated_fill?: {
    filled_at?: string
    avg_price?: number
    filled_price?: number
    quantity?: number
    filled_qty?: number
    notional?: number
    status?: string
  }
  filled_at?: string
}

type SimLedgerTradeRow = {
  capital_layer?: string
  fill_price?: number
  fill_qty?: number
  notional?: number
  order_id?: string
  realized_pnl?: number
  side?: string
  symbol?: string
  timestamp?: string
}

type SimLedgerTimelineTrade = {
  market: string
  notional: number
  strategy: string
  timestamp: string
  timestampMs: number
}

type StylePerformanceRow = {
  date?: string
  trade_date?: string
  as_of?: string
  pnl?: number | string
  realized_pnl?: number | string
  unrealized_pnl?: number | string
  max_dd?: number | string
  market?: string
  style_name?: string
  trades?: number | string
  pnl_source?: string
  capital_layer?: string
  account_type?: string
  real_execution?: boolean
}

type StyleComparisonPayload = {
  account_type?: string
  capital_layer?: string
  error_count?: number | string
  filled_count?: number | string
  generated_at?: string
  hold_count?: number | string
  market?: string
  real_execution?: boolean
  record_count?: number | string
  signal_count?: number | string
  state?: string
  style_comparison?: unknown
  style_states?: unknown
  styles_loaded?: number | string
  styles_total?: number | string
}

type StylePerformanceRecord = StylePerformanceRow & {
  marketHint: string
}

type MarketPerformanceSummary = {
  latestAt?: string
  maxDrawdown: number
  pnl: number
  realizedPnl: number
  trades: number
  unrealizedPnl: number
}

type MarketStyleSummary = {
  activeStyleCount?: number
  degradedStyleCount?: number
  errorCount?: number
  filledCount?: number
  holdCount?: number
  latestAt?: string
  recordCount?: number
  signalCount?: number
  source: string
  status?: string
  styleCount: number
  pausedStyleCount?: number
}

type EquitySnapshotRow = {
  timestamp?: string
  ts?: string
  as_of?: string
  generated_at?: string
  updated_at?: string
  date?: string
  trade_date?: string
  equity?: number | string
  total_equity?: number | string
  nav?: number | string
  net_value?: number | string
  account_value?: number | string
  portfolio_value?: number | string
  capital_base?: number | string
  initial_equity?: number | string
  starting_equity?: number | string
  start_equity?: number | string
  principal?: number | string
  pnl?: number | string
  total_pnl?: number | string
  net_pnl?: number | string
  realized_pnl?: number | string
  unrealized_pnl?: number | string
  return_pct?: number | string
  simulated_return_pct?: number | string
  target_return_pct?: number | string
  target_pct?: number | string
  benchmark_return_pct?: number | string
  benchmark_pct?: number | string
  opportunity_gap_pct?: number | string
  missed_alpha_pct?: number | string
  max_drawdown_pct?: number | string
  max_dd_pct?: number | string
  max_dd?: number | string
  drawdown?: number | string
  trade_count?: number | string
  trades?: number | string
  pnl_source?: string
  source?: string
  capital_layer?: string
  account_type?: string
  real_execution?: boolean
}

type EquitySnapshotRecord = EquitySnapshotRow & {
  sourcePath: string
}

type ParsedEquitySnapshot = {
  benchmarkPct: number
  capitalBase: number
  dayKey: string
  isSimLedgerSnapshot: boolean
  maxDrawdownPct: number
  opportunityPct: number
  pnl: number
  realizedPnl: number
  returnPct: number
  sourcePath: string
  sources: Set<string>
  targetPct: number
  timestamp: string
  timestampMs: number
  tradeCount: number
  unrealizedPnl: number
}

type TimelineContribution = {
  maxDrawdown: number
  pnl: number
  timestamp: string
  timestampMs: number
}

type PerformanceReviewRow = {
  trade_date?: string
  date?: string
  day?: string
  simulated_return_pct?: number
  return_pct?: number
  pnl_pct?: number
  mtd_return_pct?: number
  target_return_pct?: number
  target_pct?: number
  benchmark_return_pct?: number
  benchmark_pct?: number
  opportunity_gap_pct?: number
  missed_alpha_pct?: number
}

const SIGNAL_BUCKETS = ['pending', 'claimed', 'running', 'filled', 'expired', 'cancelled', 'failed', 'partial']
const MAX_SIGNALS_PER_BUCKET = 80
const MAX_SIM_LEDGER_SIGNALS = 120
const DEFAULT_TARGET_RETURN_PCT = 8
const SIM_LEDGER_EQUITY_BUCKET_MS = 5 * 60 * 1000
const MAX_EQUITY_PERFORMANCE_POINTS = 48
const DASHBOARD_MARKETS: Market[] = ['A-share', 'US', 'Crypto', 'PM', 'CNFutures']

export async function readTradingAgentSnapshot({
  workspaceRoot,
  signalQueueDir,
  now = new Date(),
}: SnapshotOptions): Promise<TradingAgentReadModelSnapshot> {
  const projectRoot = resolveTradingAgentRoot(workspaceRoot)
  const queueRoot = signalQueueDir ?? join(projectRoot, 'signals')
  const generatedAt = now.toISOString()
  const positionsPath = join(projectRoot, 'signals/positions')
  const positionPlanPath = toProjectPath(projectRoot, tradingAgentReadModelSources.capitalPlan)
  const filledSignalsPath = join(projectRoot, 'signals/filled')
  const reviewPath = toProjectPath(projectRoot, tradingAgentReadModelSources.review)
  const reviewFallbackPath = join(projectRoot, 'shared/review/data/daily_reviews.jsonl')
  const performanceTrackerRoot = join(projectRoot, 'shared/review')
  const simLedgerRoot = join(projectRoot, 'shared/logs/sim_ledger')
  const holdings = await readPositionSnapshots(positionsPath)
  const planHoldings = await readPositionPlan(positionPlanPath)
  const simLedgerHoldings = await readSimLedgerHoldings(simLedgerRoot)
  const fallbackHoldings = firstNonEmpty(holdings, planHoldings, simLedgerHoldings)
  const queueSignals = await readSignalQueue(queueRoot, now)
  const simLedgerSignals = await readSimLedgerSignals(simLedgerRoot, now)
  const signals = queueSignals.length > 0 ? queueSignals : simLedgerSignals
  const funnelEvents = buildFunnelEvents(signals)
  const reviewPerformance = firstNonEmpty(await readPerformanceSeries(reviewPath), await readPerformanceSeries(reviewFallbackPath))
  const equityPortfolio = await readEquitySnapshotPortfolio(projectRoot, generatedAt)
  const trackerPortfolio = await readStylePerformancePortfolio(performanceTrackerRoot, simLedgerRoot, generatedAt)
  const ashareAccount = await readAShareAccountSummary(projectRoot, generatedAt)
  const ashareResearchEvidence = await readAShareResearchEvidence(toProjectPath(projectRoot, tradingAgentReadModelSources.ashareResearchEvidence))
  const performance = annotatePerformanceQuality(firstNonEmpty(equityPortfolio.performance, reviewPerformance, trackerPortfolio.performance))
  const portfolio = attachAShareAccountSummary(equityPortfolio.summary ?? trackerPortfolio.summary, ashareAccount, generatedAt)
  const marketSummaries = await buildMarketSummaries({
    generatedAt,
    holdings: fallbackHoldings,
    performanceRoot: performanceTrackerRoot,
    portfolio,
    signals,
    simLedgerRoot,
  })
  const hasOrders = await directoryHasJson(filledSignalsPath)
  const hasPlan = await fileExists(positionPlanPath)
  const hasReview = await fileExists(reviewPath) || await fileExists(reviewFallbackPath)
  const hasSimLedger = simLedgerHoldings.length > 0 || simLedgerSignals.length > 0
  const hasPerformanceEvidence = hasReview || equityPortfolio.summary !== undefined || trackerPortfolio.summary !== undefined
  const performanceMessage = performance.length > 0
    ? undefined
    : hasOrders || hasPlan || hasSimLedger || hasReview
      ? '已接入交易和持仓记录；完整收益曲线等待净值或收益序列持续写入。'
      : '等待模拟盘写入收益、目标和市场基准。'

  return {
    mode: 'simulated',
    generatedAt,
    domains: {
      performance: domainHealth(performance.length > 0 ? 'ready' : 'empty', generatedAt, performanceMessage),
      signals: domainHealth(signals.length > 0 ? 'ready' : 'empty', generatedAt),
      holdings: domainHealth(fallbackHoldings.length > 0 ? 'ready' : 'empty', generatedAt),
      decisions: domainHealth(hasPerformanceEvidence ? 'ready' : 'empty', generatedAt),
      risk: domainHealth(fallbackHoldings.length > 0 || signals.length > 0 ? 'ready' : 'empty', generatedAt),
    },
    performance,
    portfolio,
    holdings: fallbackHoldings,
    signals,
    funnelEvents,
    marketSummaries,
    ashareResearchEvidence,
    sourceRefs: tradingAgentReadModelSources,
  }
}

async function readAShareAccountSummary(projectRoot: string, generatedAt: string): Promise<PortfolioSummary['ashareAccount'] | undefined> {
  const localSimDir = join(projectRoot, 'shared/logs/local_sim')
  const pnlPayload = await readOptionalJson(join(localSimDir, 'local_sim_pnl.json'))
  const pnlRows = asRecord(pnlPayload)
  const accountPnl = selectASharePnlAccount(pnlRows)
  const cashAvailable = parseFiniteNumber(accountPnl?.cash_available) ?? 0
  const marketValue = parseFiniteNumber(accountPnl?.market_value) ?? 0
  const accountTotalPnl = parseFiniteNumber(accountPnl?.total_pnl) ?? 0
  const accountEquity = roundMoney(cashAvailable + marketValue)
  const capitalBase = accountEquity - accountTotalPnl
  const sampleQuality = await readAShareSampleQuality(join(localSimDir, 'local_sim_trades.jsonl'))

  if (accountEquity <= 0 && sampleQuality.totalSampleCount === 0) return undefined

  return {
    cashAvailable: roundMoney(cashAvailable),
    marketValue: roundMoney(marketValue),
    accountEquity,
    accountTotalPnl: roundMoney(accountTotalPnl),
    accountReturnPct: roundMetric(capitalBase > 0 ? (accountTotalPnl / capitalBase) * 100 : 0),
    openPositionCount: Object.keys(accountPnl?.positions ?? {}).length,
    totalSampleCount: sampleQuality.totalSampleCount,
    validationSampleCount: sampleQuality.validationSampleCount,
    strategySampleValidCount: sampleQuality.strategySampleValidCount,
    strategyTotalPnl: sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? roundMoney(accountTotalPnl)
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    strategyMarketValue: sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? roundMoney(marketValue)
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    strategyOpenPositionCount: sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? Object.keys(accountPnl?.positions ?? {}).length
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    source: tradingAgentReadModelSources.localSimLedger,
    updatedAt: generatedAt,
  }
}

function attachAShareAccountSummary(
  summary: PortfolioSummary | undefined,
  ashareAccount: PortfolioSummary['ashareAccount'] | undefined,
  generatedAt: string,
): PortfolioSummary | undefined {
  if (!ashareAccount) return summary
  if (summary) return { ...summary, ashareAccount }
  return {
    pnlAmount: ashareAccount.accountTotalPnl,
    returnPct: ashareAccount.accountReturnPct,
    capitalBase: roundMoney(ashareAccount.accountEquity - ashareAccount.accountTotalPnl),
    targetPct: DEFAULT_TARGET_RETURN_PCT,
    maxDrawdownPct: Math.max(0, -ashareAccount.accountReturnPct),
    tradeCount: ashareAccount.totalSampleCount,
    pointCount: 1,
    source: tradingAgentReadModelSources.localSimLedger,
    pnlSource: 'ashare_local_sim_account',
    pnlCurrency: 'CNY',
    realizedPnl: 0,
    unrealizedPnl: ashareAccount.accountTotalPnl,
    ashareAccount,
    updatedAt: generatedAt,
  }
}

async function buildMarketSummaries({
  generatedAt,
  holdings,
  performanceRoot,
  portfolio,
  signals,
  simLedgerRoot,
}: {
  generatedAt: string
  holdings: HoldingRow[]
  performanceRoot: string
  portfolio?: PortfolioSummary
  signals: SignalRow[]
  simLedgerRoot: string
}): Promise<MarketSummary[]> {
  const styleSummaries = await readStyleComparisonMarketSummaries(performanceRoot)
  const performanceSummaries = await readStylePerformanceMarketSummaries(performanceRoot)
  const capitalBaseByMarket = await readSimLedgerCapitalBaseByMarket(simLedgerRoot)

  return DASHBOARD_MARKETS.map((market) => {
    const holdingCount = holdings.filter((holding) => holding.market === market).length
    const marketSignals = signals.filter((signal) => signal.market === market)
    const executedCount = marketSignals.filter((signal) => signal.status === 'executed').length
    const styleSummary = styleSummaries.get(market)
    const performanceSummary = performanceSummaries.get(market)
    const isAshare = market === 'A-share'
    const ashareAccount = isAshare ? portfolio?.ashareAccount : undefined
    const capitalBase = ashareAccount
      ? roundMoney(ashareAccount.accountEquity - ashareAccount.accountTotalPnl)
      : capitalBaseByMarket.get(market)
    const pnlAmount = ashareAccount?.accountTotalPnl ?? performanceSummary?.pnl
    const returnPct = ashareAccount
      ? ashareAccount.accountReturnPct
      : pnlAmount !== undefined && capitalBase && capitalBase > 0
        ? roundMetric((pnlAmount / capitalBase) * 100)
        : undefined
    const tradeCount = Math.max(executedCount, performanceSummary?.trades ?? 0, styleSummary?.filledCount ?? 0)
    const styleCount = Math.max(styleSummary?.styleCount ?? 0, styleSummary?.activeStyleCount ?? 0)
    const hasMeaningfulPnl = pnlAmount !== undefined && (pnlAmount !== 0 || (capitalBase ?? 0) > 0 || (performanceSummary?.trades ?? 0) > 0)
    const hasRuntime = holdingCount > 0 || marketSignals.length > 0 || tradeCount > 0 || styleCount > 0 || hasMeaningfulPnl
    const hasPartialEvidence = Boolean(performanceSummary || styleSummary)
    const hasOnlyStyleSummary = styleCount > 0 && holdingCount === 0 && marketSignals.length === 0 && pnlAmount === undefined
    const status: MarketSummary['status'] = hasRuntime ? hasOnlyStyleSummary ? 'partial' : 'ready' : hasPartialEvidence ? 'partial' : 'empty'
    const latestAt = latestIso(styleSummary?.latestAt, performanceSummary?.latestAt, ashareAccount?.updatedAt, generatedAt)

    return {
      market,
      status,
      holdingCount,
      signalCount: marketSignals.length,
      tradeCount,
      styleCount,
      activeStyleCount: styleSummary?.activeStyleCount,
      degradedStyleCount: styleSummary?.degradedStyleCount,
      pausedStyleCount: styleSummary?.pausedStyleCount,
      filledCount: styleSummary?.filledCount,
      errorCount: styleSummary?.errorCount,
      capitalBase: capitalBase === undefined ? undefined : roundMoney(capitalBase),
      pnlAmount: hasMeaningfulPnl && pnlAmount !== undefined ? roundMoney(pnlAmount) : undefined,
      returnPct,
      maxDrawdownPct: performanceSummary ? roundMetric(Math.abs(performanceSummary.maxDrawdown)) : undefined,
      realizedPnl: performanceSummary ? roundMoney(performanceSummary.realizedPnl) : undefined,
      unrealizedPnl: performanceSummary ? roundMoney(performanceSummary.unrealizedPnl) : undefined,
      latestAt,
      source: styleSummary?.source ?? (performanceSummary ? tradingAgentReadModelSources.performanceTracker : isAshare && ashareAccount ? tradingAgentReadModelSources.localSimLedger : tradingAgentReadModelSources.simLedger),
      headline: buildMarketSummaryHeadline(market, status, holdingCount, marketSignals.length, tradeCount, styleCount),
      detail: buildMarketSummaryDetail({
        activeStyleCount: styleSummary?.activeStyleCount,
        capitalBase,
        errorCount: styleSummary?.errorCount,
        filledCount: styleSummary?.filledCount,
        pnlAmount: hasMeaningfulPnl ? pnlAmount : undefined,
        returnPct,
        styleCount,
      }),
    }
  })
}

async function readStyleComparisonMarketSummaries(root: string): Promise<Map<Market, MarketStyleSummary>> {
  const summaries = new Map<Market, MarketStyleSummary>()
  try {
    const entries = await readdir(root, { withFileTypes: true })
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const path = join(root, entry.name, 'style_comparison.json')
      const payload = asRecord(await readOptionalJson(path)) as StyleComparisonPayload
      if (!Object.keys(payload).length) continue
      if (payload.real_execution === true) continue
      if (normalizeCapitalLayer(payload) !== 'simulated') continue

      const market = normalizeMarketFolder(String(payload.market ?? entry.name))
      if (market === 'All Markets' || market === 'HK') continue
      const states = summarizeStyleStates(payload.style_states)
      const comparisonCount = countStyleComparisonRows(payload.style_comparison)
      const styleCount = Math.max(
        Math.trunc(parseFiniteNumber(payload.styles_total) ?? 0),
        Math.trunc(parseFiniteNumber(payload.styles_loaded) ?? 0),
        states.total,
        comparisonCount,
      )
      const current = summaries.get(market)
      const next: MarketStyleSummary = {
        source: tradingAgentReadModelSources.styleComparison,
        status: optionalString(payload.state),
        styleCount,
        activeStyleCount: states.active,
        degradedStyleCount: states.degraded,
        pausedStyleCount: states.paused,
        filledCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.filled_count) ?? 0)),
        errorCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.error_count) ?? 0)),
        holdCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.hold_count) ?? 0)),
        recordCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.record_count) ?? 0)),
        signalCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.signal_count) ?? 0)),
        latestAt: optionalString(payload.generated_at),
      }
      summaries.set(market, current ? mergeMarketStyleSummary(current, next) : next)
    }
  } catch {
    return summaries
  }
  return summaries
}

async function readStylePerformanceMarketSummaries(root: string): Promise<Map<Market, MarketPerformanceSummary>> {
  const files = await listStylePerformanceFiles(root)
  const summaries = new Map<Market, MarketPerformanceSummary>()

  for (const file of files) {
    const market = normalizeMarketFolder(file.market)
    if (market === 'All Markets' || market === 'HK') continue
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          const row = JSON.parse(line) as StylePerformanceRow
          if (row.real_execution === true) continue
          if (normalizeCapitalLayer(row) !== 'simulated') continue
          const pnl = parseFiniteNumber(row.pnl)
          const timestamp = row.as_of ?? row.date ?? row.trade_date
          if (pnl === undefined && parseFiniteNumber(row.realized_pnl) === undefined && parseFiniteNumber(row.unrealized_pnl) === undefined) continue
          const current = summaries.get(market) ?? { maxDrawdown: 0, pnl: 0, realizedPnl: 0, trades: 0, unrealizedPnl: 0 }
          current.pnl += pnl ?? 0
          current.realizedPnl += parseFiniteNumber(row.realized_pnl) ?? 0
          current.unrealizedPnl += parseFiniteNumber(row.unrealized_pnl) ?? 0
          current.maxDrawdown = Math.max(current.maxDrawdown, Math.abs(parseFiniteNumber(row.max_dd) ?? 0))
          current.trades += Math.max(0, Math.trunc(parseFiniteNumber(row.trades) ?? 0))
          current.latestAt = latestIso(current.latestAt, timestamp)
          summaries.set(market, current)
        } catch {
          // Ignore malformed append-only rows.
        }
      }
    } catch {
      // Ignore unreadable market folders.
    }
  }

  return summaries
}

async function readSimLedgerCapitalBaseByMarket(root: string): Promise<Map<Market, number>> {
  const capitalBaseByMarket = new Map<Market, number>()
  for (const file of await listSimLedgerFiles(root, 'positions.json')) {
    try {
      const market = normalizeMarketFolder(file.market)
      if (market === 'All Markets' || market === 'HK') continue
      const payload = JSON.parse(await readFile(file.path, 'utf8')) as SimLedgerPositionsFile
      let capitalBase = parseFiniteNumber(payload.cash) ?? 0
      for (const position of Object.values(payload.positions ?? {})) {
        capitalBase += (parseFiniteNumber(position.avg_cost) ?? 0) * (parseFiniteNumber(position.quantity) ?? 0)
      }
      capitalBaseByMarket.set(market, (capitalBaseByMarket.get(market) ?? 0) + capitalBase)
    } catch {
      // Ignore malformed ledger files.
    }
  }
  return capitalBaseByMarket
}

function mergeMarketStyleSummary(current: MarketStyleSummary, next: MarketStyleSummary): MarketStyleSummary {
  return {
    source: current.source,
    status: next.status ?? current.status,
    styleCount: current.styleCount + next.styleCount,
    activeStyleCount: (current.activeStyleCount ?? 0) + (next.activeStyleCount ?? 0),
    degradedStyleCount: (current.degradedStyleCount ?? 0) + (next.degradedStyleCount ?? 0),
    pausedStyleCount: (current.pausedStyleCount ?? 0) + (next.pausedStyleCount ?? 0),
    filledCount: (current.filledCount ?? 0) + (next.filledCount ?? 0),
    errorCount: (current.errorCount ?? 0) + (next.errorCount ?? 0),
    holdCount: (current.holdCount ?? 0) + (next.holdCount ?? 0),
    recordCount: (current.recordCount ?? 0) + (next.recordCount ?? 0),
    signalCount: (current.signalCount ?? 0) + (next.signalCount ?? 0),
    latestAt: latestIso(current.latestAt, next.latestAt),
  }
}

function summarizeStyleStates(value: unknown) {
  const states = asRecord(value)
  let active = 0
  let degraded = 0
  let paused = 0
  let total = 0

  for (const [key, raw] of Object.entries(states)) {
    const numeric = parseFiniteNumber(raw as number | string | undefined)
    if (numeric !== undefined && ['active', 'degraded', 'paused'].includes(key.toLowerCase())) {
      const count = Math.max(0, Math.trunc(numeric))
      total += count
      if (key.toLowerCase() === 'active') active += count
      if (key.toLowerCase() === 'degraded') degraded += count
      if (key.toLowerCase() === 'paused') paused += count
      continue
    }

    const state = String(raw ?? '').toLowerCase()
    if (!state) continue
    total += 1
    if (state.includes('active') || state.includes('ready')) active += 1
    else if (state.includes('degraded') || state.includes('warn')) degraded += 1
    else if (state.includes('paused') || state.includes('disabled')) paused += 1
  }

  return { active, degraded, paused, total }
}

function countStyleComparisonRows(value: unknown) {
  if (Array.isArray(value)) return value.length
  const rows = asRecord(value)
  return Object.keys(rows).length
}

function normalizeMarketFolder(value: string): Market {
  return normalizeMarket(value, value.toUpperCase())
}

function buildMarketSummaryHeadline(market: Market, status: MarketSummary['status'], holdingCount: number, signalCount: number, tradeCount: number, styleCount: number) {
  if (status === 'empty') return `${marketName(market)}暂无模拟记录`
  if (tradeCount > 0) return `${marketName(market)}已有 ${tradeCount} 笔模拟成交`
  if (signalCount > 0) return `${marketName(market)}有 ${signalCount} 条机会记录`
  if (holdingCount > 0) return `${marketName(market)}有 ${holdingCount} 个持仓`
  if (styleCount > 0) return `${marketName(market)}风格运行中`
  return `${marketName(market)}等待数据`
}

function buildMarketSummaryDetail({
  activeStyleCount,
  capitalBase,
  errorCount,
  filledCount,
  pnlAmount,
  returnPct,
  styleCount,
}: {
  activeStyleCount?: number
  capitalBase?: number
  errorCount?: number
  filledCount?: number
  pnlAmount?: number
  returnPct?: number
  styleCount: number
}) {
  const facts: string[] = []
  if (pnlAmount !== undefined) facts.push(`收益 ${formatSignedAmount(pnlAmount)}`)
  if (returnPct !== undefined) facts.push(`回报 ${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%`)
  if (capitalBase !== undefined && capitalBase > 0) facts.push(`资金 ${formatCompactAmount(capitalBase)}`)
  if (styleCount > 0) facts.push(`风格 ${activeStyleCount ?? 0}/${styleCount}`)
  if (filledCount !== undefined) facts.push(`成交 ${filledCount}`)
  if (errorCount) facts.push(`失败 ${errorCount}`)
  return facts.length ? facts.join(' · ') : '等待该市场写入模拟成交、持仓或风格收益。'
}

function marketName(market: Market) {
  if (market === 'A-share') return 'A股'
  if (market === 'US') return '美股'
  if (market === 'Crypto') return '加密'
  if (market === 'PM') return '预测'
  if (market === 'CNFutures') return '中国期货'
  if (market === 'HK') return '港股'
  return '全市场'
}

function latestIso(...values: Array<string | undefined>) {
  let latest: string | undefined
  let latestMs = Number.NEGATIVE_INFINITY
  for (const value of values) {
    if (!value) continue
    const ms = Date.parse(value)
    if (!Number.isFinite(ms)) continue
    if (ms > latestMs) {
      latest = value
      latestMs = ms
    }
  }
  return latest
}

function formatSignedAmount(value: number) {
  const sign = value >= 0 ? '+' : '-'
  return `${sign}${formatCompactAmount(Math.abs(value))}`
}

function formatCompactAmount(value: number) {
  const abs = Math.abs(value)
  if (abs >= 100_000_000) return `${(value / 100_000_000).toFixed(2)}亿`
  if (abs >= 10_000) return `${(value / 10_000).toFixed(2)}万`
  return `${Math.round(value).toLocaleString('zh-CN')}`
}

async function readOptionalJson(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, 'utf8')) as unknown
  } catch {
    return undefined
  }
}

function selectASharePnlAccount(payload: Record<string, unknown>): LocalSimAccountPnl | undefined {
  const direct = asRecord(payload.ashare_sim) as LocalSimAccountPnl
  if (isLocalSimAccountPnl(direct)) return direct

  for (const [key, row] of Object.entries(payload)) {
    if (!/a-?share|ashare|cn_?stock/i.test(key)) continue
    const account = asRecord(row) as LocalSimAccountPnl
    if (isLocalSimAccountPnl(account)) return account
  }

  const root = payload as LocalSimAccountPnl
  if (isLocalSimAccountPnl(root)) return root
  return undefined
}

function isLocalSimAccountPnl(value: LocalSimAccountPnl | undefined) {
  if (!value) return false
  return value.cash_available !== undefined || value.market_value !== undefined || value.total_pnl !== undefined || value.positions !== undefined
}

async function readAShareSampleQuality(path: string) {
  const rows: LocalSimTradeRow[] = []
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    for (const line of lines) {
      try {
        const row = JSON.parse(line) as LocalSimTradeRow
        if (row.status && String(row.status).toLowerCase() !== 'filled') continue
        rows.push(row)
      } catch {
        // Ignore malformed append-only rows.
      }
    }
  } catch {
    return { totalSampleCount: 0, validationSampleCount: 0, strategySampleValidCount: 0 }
  }

  const strategySampleValidCount = rows.filter(isAshareStrategySample).length
  return {
    totalSampleCount: rows.length,
    validationSampleCount: rows.length - strategySampleValidCount,
    strategySampleValidCount,
  }
}

function isAshareStrategySample(row: LocalSimTradeRow) {
  const market = String(row.market ?? 'ashare').toLowerCase()
  const side = String(row.side ?? '').toLowerCase()
  const source = String(row.execution_source ?? '').toLowerCase()
  const layer = String(row.candidate_pool_layer ?? '').toLowerCase()
  if (market !== 'ashare') return false
  if (side === 'buy') return source === 'ashare_candidate_layer' && layer === 'candidate'
  if (side === 'sell') return source === 'ashare_rebalance_sell'
  return false
}

function resolveTradingAgentRoot(workspaceRoot: string) {
  return basename(workspaceRoot).toLowerCase() === 'tradingagent' ? workspaceRoot : join(workspaceRoot, 'TradingAgent')
}

function toProjectPath(projectRoot: string, sourceRef: string) {
  return join(projectRoot, sourceRef.replace(/^TradingAgent\//, ''))
}

async function readPerformanceSeries(path: string): Promise<PerformancePoint[]> {
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    return lines
      .map((line) => parsePerformanceRow(JSON.parse(line) as PerformanceReviewRow))
      .filter((row): row is PerformancePoint => Boolean(row))
  } catch {
    return []
  }
}

function annotatePerformanceQuality(performance: PerformancePoint[]): PerformancePoint[] {
  if (performance.length < 8) return performance

  const latest = performance.at(-1)!
  const highWater = Math.max(...performance.map((point) => point.simulated))
  const hasLargeRebase = highWater - latest.simulated >= 35
  if (!hasLargeRebase) return performance

  return performance.map((point, index) => {
    const previous = performance[index - 1]
    const next = performance[index + 1]
    const isHighPlateau = point.simulated - latest.simulated >= 30 && point.simulated >= DEFAULT_TARGET_RETURN_PCT + 35
    const isJump = previous ? Math.abs(point.simulated - previous.simulated) >= 35 : false
    const isDrop = next ? point.simulated - next.simulated >= 35 : false

    if (!isHighPlateau && !isJump && !isDrop) return point

    return {
      ...point,
      quality: 'outlier' as const,
      qualityReason: '口径跳变候选',
    }
  })
}

async function readAShareResearchEvidence(path: string): Promise<AShareResearchEvidence | undefined> {
  try {
    const payload = await readJson(path) as Record<string, unknown>
    const opening = asRecord(payload.opening_auction)
    const closing = asRecord(payload.closing_momentum)
    const reverseRepo = asRecord(payload.reverse_repo)
    const styleEvidence = asRecord(payload.style_evidence)
    const styleSummary = asRecord(styleEvidence.summary)
    const candidates = Array.isArray(closing.candidates) ? closing.candidates : []

    return {
      generatedAt: String(payload.generated_at ?? ''),
      tradeDate: String(payload.trade_date ?? ''),
      readOnly: payload.read_only === true,
      realTradingEnabled: payload.real_trading_enabled === true,
      openingAuction: {
        state: String(opening.state ?? 'unknown'),
        phase: String(opening.phase ?? 'unknown'),
        dataMode: optionalString(opening.data_mode),
        anomalyCount: Math.max(0, Math.trunc(parseFiniteNumber(opening.anomaly_count as number | string | undefined) ?? 0)),
        symbolsWithBars: Math.max(0, Math.trunc(parseFiniteNumber(opening.symbols_with_bars as number | string | undefined) ?? 0)),
        proxySymbolsWithBars: Math.max(0, Math.trunc(parseFiniteNumber(opening.proxy_symbols_with_bars as number | string | undefined) ?? 0)),
      },
      closingMomentum: {
        state: String(closing.state ?? 'unknown'),
        candidateCount: Math.max(0, Math.trunc(parseFiniteNumber(closing.candidate_count as number | string | undefined) ?? 0)),
        symbolsWithBars: Math.max(0, Math.trunc(parseFiniteNumber(closing.symbols_with_bars as number | string | undefined) ?? 0)),
        candidates: candidates.slice(0, 5).map((candidate) => {
          const row = asRecord(candidate)
          return {
            symbol: String(row.symbol ?? ''),
            tailMomentum: parseFiniteNumber(row.tail_momentum as number | string | undefined),
            volumeRatio: parseFiniteNumber(row.volume_ratio as number | string | undefined),
            labelState: optionalString(row.label_state),
            nextDayOpenReturn: parseNullableNumber(row.next_day_open_return),
            nextDayHighReturn: parseNullableNumber(row.next_day_high_return),
          }
        }).filter((candidate) => candidate.symbol),
      },
      reverseRepo: {
        action: String(reverseRepo.action ?? 'skip'),
        amount: parseFiniteNumber(reverseRepo.amount as number | string | undefined) ?? 0,
        lots: Math.max(0, Math.trunc(parseFiniteNumber(reverseRepo.lots as number | string | undefined) ?? 0)),
        annualizedYield: parseFiniteNumber(reverseRepo.annualized_yield as number | string | undefined) ?? 0,
        yieldSource: optionalString(reverseRepo.yield_source),
        estimatedInterest: parseFiniteNumber(reverseRepo.estimated_interest as number | string | undefined) ?? 0,
      },
      styleEvidence: {
        summary: {
          styles: Math.max(0, Math.trunc(parseFiniteNumber(styleSummary.styles as number | string | undefined) ?? 0)),
          activeSample: parseFiniteNumber(styleSummary.active_sample as number | string | undefined),
          degraded: parseFiniteNumber(styleSummary.degraded as number | string | undefined),
          paused: parseFiniteNumber(styleSummary.paused as number | string | undefined),
          virtualCapital: parseFiniteNumber(styleSummary.virtual_capital as number | string | undefined),
          allocatedCapital: parseFiniteNumber(styleSummary.allocated_capital as number | string | undefined),
          unallocatedCapital: parseFiniteNumber(styleSummary.unallocated_capital as number | string | undefined),
        },
      },
    }
  } catch {
    return undefined
  }
}

async function readEquitySnapshotPortfolio(projectRoot: string, generatedAt: string): Promise<{
  performance: PerformancePoint[]
  summary?: PortfolioSummary
}> {
  const files = await listEquitySnapshotFiles(projectRoot)
  const rows: EquitySnapshotRecord[] = []

  for (const file of files) {
    try {
      const lines = (await readFile(file, 'utf8')).trim().split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          rows.push({ ...(JSON.parse(line) as EquitySnapshotRow), sourcePath: file })
        } catch {
          // Ignore malformed append-only rows; other equity snapshots remain usable.
        }
      }
    } catch {
      // Ignore unreadable optional sources.
    }
  }

  const snapshots = rows
    .filter((row) => row.real_execution !== true)
    .filter((row) => normalizeCapitalLayer(row) === 'simulated')
    .map(parseEquitySnapshotRecord)
    .filter((row): row is ParsedEquitySnapshot => Boolean(row))
    .sort((a, b) => a.timestampMs - b.timestampMs)

  if (!snapshots.length) return { performance: [] }

  const grouped = snapshots.some((snapshot) => snapshot.isSimLedgerSnapshot)
    ? groupSimLedgerEquitySnapshots(snapshots.filter((snapshot) => snapshot.isSimLedgerSnapshot))
    : groupEquitySnapshotsByTimestamp(snapshots)

  const timestamps = [...grouped.keys()].sort((a, b) => a - b)
  const snapshotRows = timestamps.map((timestampMs) => grouped.get(timestampMs)!)
  const useProgressiveTarget = shouldUseProgressiveEquityTarget(snapshotRows)
  const performance = timestamps.map((_, index) => {
    const row = snapshotRows[index]
    const simulated = row.capitalBase > 0 ? (row.pnl / row.capitalBase) * 100 : row.returnPct
    const target = useProgressiveTarget
      ? Math.min(DEFAULT_TARGET_RETURN_PCT, DEFAULT_TARGET_RETURN_PCT * ((index + 1) / timestamps.length))
      : row.targetPct > 0 ? row.targetPct : Math.min(DEFAULT_TARGET_RETURN_PCT, DEFAULT_TARGET_RETURN_PCT * ((index + 1) / timestamps.length))

    return {
      day: index === timestamps.length - 1 ? '现在' : formatTimelineLabel(row.timestamp),
      simulated: roundMetric(simulated),
      target: roundMetric(target),
      benchmark: roundMetric(row.benchmarkPct),
      opportunity: roundMetric(row.opportunityPct),
    }
  })
  const latest = grouped.get(timestamps.at(-1)!)!

  return {
    performance,
    summary: {
      pnlAmount: roundMoney(latest.pnl),
      returnPct: roundMetric(latest.capitalBase > 0 ? (latest.pnl / latest.capitalBase) * 100 : latest.returnPct),
      capitalBase: roundMoney(latest.capitalBase),
      targetPct: latest.targetPct > 0 ? roundMetric(latest.targetPct) : DEFAULT_TARGET_RETURN_PCT,
      maxDrawdownPct: roundMetric(latest.maxDrawdownPct),
      tradeCount: latest.tradeCount,
      pointCount: performance.length,
      source: tradingAgentReadModelSources.equitySnapshots,
      pnlSource: latest.sources.size === 1 ? [...latest.sources][0] : latest.sources.size > 1 ? 'mixed' : 'equity_snapshot',
      realizedPnl: roundMoney(latest.realizedPnl),
      unrealizedPnl: roundMoney(latest.unrealizedPnl),
      updatedAt: generatedAt,
    },
  }
}

function shouldUseProgressiveEquityTarget(rows: ParsedEquitySnapshot[]) {
  if (rows.length <= 2) return false
  if (!rows.every((row) => row.isSimLedgerSnapshot)) return false
  const positiveTargets = rows.map((row) => row.targetPct).filter((value) => value > 0)
  if (positiveTargets.length !== rows.length) return true
  return new Set(positiveTargets.map((value) => roundMetric(value))).size === 1
}

function groupEquitySnapshotsByTimestamp(snapshots: ParsedEquitySnapshot[]) {
  const grouped = new Map<number, ParsedEquitySnapshot>()
  for (const snapshot of snapshots) {
    const current = grouped.get(snapshot.timestampMs)
    if (!current) {
      grouped.set(snapshot.timestampMs, { ...snapshot })
      continue
    }

    mergeEquitySnapshot(current, snapshot)
  }
  return grouped
}

function groupSimLedgerEquitySnapshots(snapshots: ParsedEquitySnapshot[]) {
  const latestByBucketAndSource = new Map<string, ParsedEquitySnapshot>()
  for (const snapshot of snapshots) {
    const bucketMs = bucketSimLedgerTimestamp(snapshot.timestampMs)
    const key = `${bucketMs}|${snapshot.sourcePath}`
    const current = latestByBucketAndSource.get(key)
    if (!current || snapshot.timestampMs >= current.timestampMs) latestByBucketAndSource.set(key, snapshot)
  }

  const snapshotsByBucket = new Map<number, ParsedEquitySnapshot[]>()
  for (const snapshot of latestByBucketAndSource.values()) {
    const bucketMs = bucketSimLedgerTimestamp(snapshot.timestampMs)
    snapshotsByBucket.set(bucketMs, [...(snapshotsByBucket.get(bucketMs) ?? []), snapshot])
  }

  const groupedByBucket = new Map<number, ParsedEquitySnapshot>()
  const latestBySource = new Map<string, ParsedEquitySnapshot>()
  for (const bucketMs of [...snapshotsByBucket.keys()].sort((a, b) => a - b)) {
    for (const snapshot of snapshotsByBucket.get(bucketMs) ?? []) {
      latestBySource.set(snapshot.sourcePath, snapshot)
    }

    let merged: ParsedEquitySnapshot | undefined
    for (const snapshot of [...latestBySource.values()].sort((a, b) => a.sourcePath.localeCompare(b.sourcePath))) {
      if (!merged) {
        merged = cloneEquitySnapshot(snapshot)
        continue
      }
      if (snapshot.timestampMs > merged.timestampMs) {
        merged.timestamp = snapshot.timestamp
        merged.timestampMs = snapshot.timestampMs
      }
      mergeEquitySnapshot(merged, snapshot)
    }
    if (merged) groupedByBucket.set(bucketMs, merged)
  }

  return limitPerformanceGroups(groupedByBucket, MAX_EQUITY_PERFORMANCE_POINTS)
}

function cloneEquitySnapshot(snapshot: ParsedEquitySnapshot): ParsedEquitySnapshot {
  return { ...snapshot, sources: new Set(snapshot.sources) }
}

function bucketSimLedgerTimestamp(timestampMs: number) {
  return Math.floor(timestampMs / SIM_LEDGER_EQUITY_BUCKET_MS) * SIM_LEDGER_EQUITY_BUCKET_MS
}

function limitPerformanceGroups(grouped: Map<number, ParsedEquitySnapshot>, maxPoints: number) {
  const entries = [...grouped.entries()].sort(([a], [b]) => a - b)
  if (entries.length <= maxPoints) return new Map(entries)

  const selected = new Map<number, ParsedEquitySnapshot>()
  const lastIndex = entries.length - 1
  for (let index = 0; index < maxPoints; index += 1) {
    const sourceIndex = index === maxPoints - 1
      ? lastIndex
      : Math.round((index / (maxPoints - 1)) * lastIndex)
    const [timestampMs, snapshot] = entries[sourceIndex]
    selected.set(timestampMs, snapshot)
  }
  return selected
}

function mergeEquitySnapshot(current: ParsedEquitySnapshot, snapshot: ParsedEquitySnapshot) {
  const previousCapitalBase = current.capitalBase
  current.capitalBase += snapshot.capitalBase
  current.pnl += snapshot.pnl
  current.realizedPnl += snapshot.realizedPnl
  current.unrealizedPnl += snapshot.unrealizedPnl
  current.tradeCount += snapshot.tradeCount
  current.maxDrawdownPct = Math.max(current.maxDrawdownPct, snapshot.maxDrawdownPct)
  current.benchmarkPct = weightedAverage(current.benchmarkPct, previousCapitalBase, snapshot.benchmarkPct, snapshot.capitalBase)
  current.opportunityPct = weightedAverage(current.opportunityPct, previousCapitalBase, snapshot.opportunityPct, snapshot.capitalBase)
  current.targetPct = Math.max(current.targetPct, snapshot.targetPct)
  for (const source of snapshot.sources) current.sources.add(source)
}

async function listEquitySnapshotFiles(projectRoot: string): Promise<string[]> {
  const reviewRoot = join(projectRoot, 'shared/review')
  const simLedgerRoot = join(projectRoot, 'shared/logs/sim_ledger')
  const files: string[] = []

  for (const folder of ['portfolio', 'daily']) {
    for (const name of ['equity_snapshots.jsonl', 'equity_series.jsonl']) {
      const candidate = join(reviewRoot, folder, name)
      if (await fileExists(candidate)) files.push(candidate)
    }
  }

  try {
    const entries = await readdir(reviewRoot, { withFileTypes: true })
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name === 'portfolio' || entry.name === 'daily') continue
      for (const name of ['equity_snapshots.jsonl', 'equity_series.jsonl']) {
        const candidate = join(reviewRoot, entry.name, name)
        if (await fileExists(candidate)) files.push(candidate)
      }
    }
  } catch {
    // Review root is optional in fresh workspaces.
  }

  for (const targetName of ['daily_mark_to_market.jsonl', 'equity_snapshots.jsonl'] as const) {
    for (const file of await listSimLedgerFiles(simLedgerRoot, targetName)) {
      files.push(file.path)
    }
  }

  return [...new Set(files)]
}

function parseEquitySnapshotRecord(row: EquitySnapshotRecord): ParsedEquitySnapshot | null {
  const timestamp = row.timestamp ?? row.ts ?? row.as_of ?? row.generated_at ?? row.updated_at ?? row.date ?? row.trade_date
  if (!timestamp) return null
  const timestampMs = parseSnapshotTimestamp(timestamp)
  if (!Number.isFinite(timestampMs)) return null
  const dayKey = compactDate(row.date ?? row.trade_date ?? String(timestamp)) ?? compactDate(formatDateForSnapshot(timestampMs))
  if (!dayKey) return null

  const equity = firstParsedNumber(row.total_equity, row.equity, row.nav, row.net_value, row.account_value, row.portfolio_value)
  const capitalBase = firstParsedNumber(row.capital_base, row.initial_equity, row.starting_equity, row.start_equity, row.principal)
  const realizedPnl = parseFiniteNumber(row.realized_pnl) ?? 0
  const unrealizedPnl = parseFiniteNumber(row.unrealized_pnl) ?? 0
  const explicitPnl = firstParsedNumber(row.pnl, row.total_pnl, row.net_pnl)
  const pnl = explicitPnl ?? (realizedPnl || unrealizedPnl ? realizedPnl + unrealizedPnl : equity !== undefined && capitalBase !== undefined ? equity - capitalBase : undefined)
  const returnPct = firstParsedNumber(row.simulated_return_pct, row.return_pct)

  if (pnl === undefined && returnPct === undefined) return null

  const base = capitalBase ?? (equity !== undefined && pnl !== undefined ? equity - pnl : undefined)
  if (base === undefined || base <= 0) return null

  const drawdownPct = firstParsedNumber(row.max_drawdown_pct, row.max_dd_pct)
  const drawdownAmount = firstParsedNumber(row.max_dd, row.drawdown)

  return {
    benchmarkPct: firstParsedNumber(row.benchmark_return_pct, row.benchmark_pct) ?? 0,
    capitalBase: base,
    dayKey,
    isSimLedgerSnapshot: row.sourcePath.includes('/shared/logs/sim_ledger/'),
    maxDrawdownPct: Math.abs(drawdownPct ?? (drawdownAmount !== undefined ? (drawdownAmount / base) * 100 : 0)),
    opportunityPct: firstParsedNumber(row.opportunity_gap_pct, row.missed_alpha_pct) ?? 0,
    pnl: pnl ?? (returnPct! / 100) * base,
    realizedPnl,
    returnPct: returnPct ?? 0,
    sourcePath: row.sourcePath,
    sources: new Set([row.pnl_source ?? row.source ?? 'equity_snapshot']),
    targetPct: firstParsedNumber(row.target_return_pct, row.target_pct) ?? 0,
    timestamp: String(timestamp),
    timestampMs,
    tradeCount: Math.max(0, Math.trunc(firstParsedNumber(row.trade_count, row.trades) ?? 0)),
    unrealizedPnl,
  }
}

async function readStylePerformancePortfolio(root: string, simLedgerRoot: string, generatedAt: string): Promise<{
  performance: PerformancePoint[]
  summary?: PortfolioSummary
}> {
  const files = await listStylePerformanceFiles(root)
  const rows: StylePerformanceRecord[] = []

  for (const file of files) {
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          rows.push({ ...(JSON.parse(line) as StylePerformanceRow), marketHint: file.market })
        } catch {
          // Ignore malformed append-only rows; other rows remain usable.
        }
      }
    } catch {
      // Ignore unreadable market folders.
    }
  }

  const tradeTimeline = await readSimLedgerTradeTimeline(simLedgerRoot)
  const timelineContributions: TimelineContribution[] = []
  const byDay = new Map<string, { pnl: number; realizedPnl: number; unrealizedPnl: number; maxDrawdown: number; trades: number; pnlSources: Set<string> }>()
  for (const row of rows) {
    if (row.real_execution === true) continue
    if (normalizeCapitalLayer(row) !== 'simulated') continue
    const dayKey = compactDate(row.date ?? row.trade_date ?? row.as_of)
    const pnl = parseFiniteNumber(row.pnl)
    if (!dayKey || pnl === undefined) continue
    const current = byDay.get(dayKey) ?? { pnl: 0, realizedPnl: 0, unrealizedPnl: 0, maxDrawdown: 0, trades: 0, pnlSources: new Set<string>() }
    const maxDrawdown = parseFiniteNumber(row.max_dd) ?? 0
    current.pnl += pnl
    current.realizedPnl += parseFiniteNumber(row.realized_pnl) ?? 0
    current.unrealizedPnl += parseFiniteNumber(row.unrealized_pnl) ?? 0
    current.maxDrawdown = Math.max(current.maxDrawdown, maxDrawdown)
    current.trades += Math.max(0, Math.trunc(parseFiniteNumber(row.trades) ?? 0))
    if (row.pnl_source) current.pnlSources.add(String(row.pnl_source))
    byDay.set(dayKey, current)

    const matchingTrades = tradeTimeline.get(performanceTradeKey(dayKey, row.market ?? row.marketHint, row.style_name))
    if (matchingTrades?.length && pnl !== 0) {
      const totalNotional = matchingTrades.reduce((sum, trade) => sum + Math.max(0, trade.notional), 0)
      const equalShare = 1 / matchingTrades.length
      for (const trade of matchingTrades) {
        const weight = totalNotional > 0 ? Math.max(0, trade.notional) / totalNotional : equalShare
        timelineContributions.push({
          maxDrawdown: maxDrawdown * weight,
          pnl: pnl * weight,
          timestamp: trade.timestamp,
          timestampMs: trade.timestampMs,
        })
      }
    }
  }

  if (!byDay.size) return { performance: [] }

  const capitalBase = await readSimLedgerCapitalBase(simLedgerRoot)
  const dates = [...byDay.keys()].sort()
  let cumulativePnl = 0
  const dailyPerformance = dates.map((day, index) => {
    const value = byDay.get(day)!
    cumulativePnl += value.pnl
    const simulated = capitalBase > 0 ? (cumulativePnl / capitalBase) * 100 : 0
    const target = Math.min(DEFAULT_TARGET_RETURN_PCT, DEFAULT_TARGET_RETURN_PCT * ((index + 1) / dates.length))
    const drawdown = capitalBase > 0 ? (Math.abs(value.maxDrawdown) / capitalBase) * 100 : 0

    return {
      day: index === dates.length - 1 ? '现在' : formatReviewDay(day) ?? day,
      simulated: roundMetric(simulated),
      target: roundMetric(target),
      benchmark: 0,
      opportunity: roundMetric(-drawdown),
    }
  })
  const timelinePerformance = buildTimelinePerformance(timelineContributions, capitalBase)
  const performance = timelinePerformance.length > dailyPerformance.length ? timelinePerformance : dailyPerformance
  const totalPnl = [...byDay.values()].reduce((sum, row) => sum + row.pnl, 0)
  const totalRealizedPnl = [...byDay.values()].reduce((sum, row) => sum + row.realizedPnl, 0)
  const totalUnrealizedPnl = [...byDay.values()].reduce((sum, row) => sum + row.unrealizedPnl, 0)
  const pnlSources = new Set([...byDay.values()].flatMap((row) => [...row.pnlSources]))
  const latest = byDay.get(dates.at(-1)!)!
  const maxDrawdown = Math.max(...[...byDay.values()].map((row) => Math.abs(row.maxDrawdown)), 0)

  return {
    performance,
    summary: {
      pnlAmount: roundMoney(totalPnl),
      returnPct: roundMetric(capitalBase > 0 ? (totalPnl / capitalBase) * 100 : 0),
      capitalBase: roundMoney(capitalBase),
      targetPct: DEFAULT_TARGET_RETURN_PCT,
      maxDrawdownPct: roundMetric(capitalBase > 0 ? (maxDrawdown / capitalBase) * 100 : 0),
      tradeCount: latest.trades,
      pointCount: performance.length,
      source: tradingAgentReadModelSources.performanceTracker,
      pnlSource: pnlSources.size === 1 ? [...pnlSources][0] : pnlSources.size > 1 ? 'mixed' : undefined,
      realizedPnl: roundMoney(totalRealizedPnl),
      unrealizedPnl: roundMoney(totalUnrealizedPnl),
      updatedAt: generatedAt,
    },
  }
}

async function listStylePerformanceFiles(root: string) {
  const files: Array<{ path: string; market: string }> = []
  try {
    const entries = await readdir(root, { withFileTypes: true })
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const path = join(root, entry.name, 'style_performance.jsonl')
      if (await fileExists(path)) files.push({ path, market: entry.name })
    }
  } catch {
    return []
  }
  return files
}

function buildTimelinePerformance(contributions: TimelineContribution[], capitalBase: number): PerformancePoint[] {
  const usable = contributions
    .filter((row) => Number.isFinite(row.timestampMs) && Number.isFinite(row.pnl))
    .sort((a, b) => a.timestampMs - b.timestampMs)
  if (!usable.length) return []

  const byTimestamp = new Map<number, { timestamp: string; pnl: number; maxDrawdown: number }>()
  for (const row of usable) {
    const current = byTimestamp.get(row.timestampMs) ?? { timestamp: row.timestamp, pnl: 0, maxDrawdown: 0 }
    current.pnl += row.pnl
    current.maxDrawdown += Math.abs(row.maxDrawdown)
    byTimestamp.set(row.timestampMs, current)
  }

  const timestamps = [...byTimestamp.keys()].sort((a, b) => a - b)
  let cumulativePnl = 0
  return timestamps.map((timestampMs, index) => {
    const row = byTimestamp.get(timestampMs)!
    cumulativePnl += row.pnl
    const simulated = capitalBase > 0 ? (cumulativePnl / capitalBase) * 100 : 0
    const target = Math.min(DEFAULT_TARGET_RETURN_PCT, DEFAULT_TARGET_RETURN_PCT * ((index + 1) / timestamps.length))
    const drawdown = capitalBase > 0 ? (row.maxDrawdown / capitalBase) * 100 : 0

    return {
      day: index === timestamps.length - 1 ? '现在' : formatTimelineLabel(row.timestamp),
      simulated: roundMetric(simulated),
      target: roundMetric(target),
      benchmark: 0,
      opportunity: roundMetric(-drawdown),
    }
  })
}

function parsePerformanceRow(row: PerformanceReviewRow): PerformancePoint | null {
  const day = formatReviewDay(row.trade_date ?? row.date ?? row.day)
  const simulated = firstNumber(row.simulated_return_pct, row.return_pct, row.pnl_pct, row.mtd_return_pct)
  if (!day || simulated === undefined) return null

  return {
    day,
    simulated,
    target: firstNumber(row.target_return_pct, row.target_pct) ?? 0,
    benchmark: firstNumber(row.benchmark_return_pct, row.benchmark_pct) ?? 0,
    opportunity: firstNumber(row.opportunity_gap_pct, row.missed_alpha_pct) ?? 0,
  }
}

async function readPositionSnapshots(root: string): Promise<HoldingRow[]> {
  try {
    const names = await readdir(root)
    const rows = await Promise.all(
      names.filter((name) => name.endsWith('.json')).map(async (name) => parsePositionSnapshot(await readJson(join(root, name)))),
    )
    return rows.flat().filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

async function readPositionPlan(path: string): Promise<HoldingRow[]> {
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    const latest = JSON.parse(lines.at(-1) ?? '{}') as PositionPlanFile
    return (latest.positions ?? []).map(parsePositionRow).filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return readLegacySimulatedPositions(path)
  }
}

async function readSimLedgerHoldings(root: string): Promise<HoldingRow[]> {
  const files = await listSimLedgerFiles(root, 'positions.json')
  const rows = await Promise.all(files.map(async (file) => {
    try {
      const payload = JSON.parse(await readFile(file.path, 'utf8')) as SimLedgerPositionsFile
      return Object.entries(payload.positions ?? {}).map(([symbol, position]) => parseSimLedgerPosition(symbol, position, file.market, file.strategy))
    } catch {
      return []
    }
  }))

  return rows.flat().filter((row): row is HoldingRow => Boolean(row))
}

async function readSimLedgerCapitalBase(root: string): Promise<number> {
  const files = await listSimLedgerFiles(root, 'positions.json')
  let capitalBase = 0

  for (const file of files) {
    try {
      const payload = JSON.parse(await readFile(file.path, 'utf8')) as SimLedgerPositionsFile
      capitalBase += parseFiniteNumber(payload.cash) ?? 0
      for (const position of Object.values(payload.positions ?? {})) {
        capitalBase += (parseFiniteNumber(position.avg_cost) ?? 0) * (parseFiniteNumber(position.quantity) ?? 0)
      }
    } catch {
      // Ignore malformed ledger files; the snapshot API is read-only and should remain resilient.
    }
  }

  return capitalBase
}

function readLegacySimulatedPositions(path: string): HoldingRow[] {
  try {
    const db = new DatabaseSync(path, { readOnly: true })
    const rows = db.prepare('SELECT * FROM position_ledger_simulated').all() as PositionRow[]
    db.close()

    return rows.map(parsePositionRow).filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

function parseSimLedgerPosition(symbol: string, position: SimLedgerPosition, marketHint: string, strategy: string): HoldingRow | null {
  if (!symbol || !position.quantity) return null
  const market = normalizeMarket(marketHint, symbol)
  const cost = Number(position.avg_cost ?? 0) * Number(position.quantity ?? 0)

  return {
    symbol: normalizeSymbol(symbol, market),
    name: normalizeSymbol(symbol, market),
    market,
    weight: formatCost(cost),
    pnl: formatCurrency(position.realized_pnl ?? 0),
    risk: position.quantity > 0 ? '正常' : '观察',
    role: `${formatStrategyName(strategy)} 持仓`,
  }
}

function parsePositionSnapshot(payload: unknown): HoldingRow[] {
  if (Array.isArray(payload)) {
    return payload.map(parsePositionRow).filter((row): row is HoldingRow => Boolean(row))
  }

  const direct = parsePositionRow(payload)
  if (direct) return [direct]

  const snapshot = payload as CNFuturesPositionsFile
  if (Array.isArray(snapshot.positions)) {
    return snapshot.positions
      .map((row) => (asRecord(row).ts_code ? parsePositionRow(row) : parseCNFuturesPositionRow(row)))
      .filter((row): row is HoldingRow => Boolean(row))
  }

  return []
}

function parseCNFuturesPositionRow(row: CNFuturesPositionRow): HoldingRow | null {
  const symbol = String(row.symbol ?? '').trim()
  if (!symbol) return null
  const qty = parseFiniteNumber(row.net_qty) ?? 0
  if (qty === 0) return null
  const margin = parseFiniteNumber(row.margin_required)
  const realized = parseFiniteNumber(row.realized_pnl) ?? 0
  const unrealized = parseFiniteNumber(row.unrealized_pnl) ?? 0
  const style = row.style ? formatStrategyName(String(row.style)) : '期货模拟'

  return {
    symbol,
    name: symbol,
    market: 'CNFutures',
    weight: margin === undefined ? `${qty} 手` : formatCurrency(margin),
    pnl: formatCurrency(realized + unrealized),
    risk: qty > 0 ? '正常' : '观察',
    role: `${style} 持仓`,
  }
}

function parsePositionRow(row: unknown): HoldingRow | null {
  const position = row as PositionRow
  const symbol = position.ts_code
  if (!symbol) return null
  const market = inferMarket(symbol)
  const marketValue = parseFiniteNumber(position.market_value)
  const runningCost = firstParsedNumber(position.cost_basis, position.running_cost)
  const quantity = parseFiniteNumber(position.quantity)
  const realizedPnl = parseFiniteNumber(position.realized_pnl) ?? 0
  const unrealizedPnl = parseFiniteNumber(position.unrealized_pnl)
  const pnl = firstParsedNumber(position.pnl, unrealizedPnl === undefined ? undefined : realizedPnl + unrealizedPnl, position.realized_pnl) ?? 0

  return {
    symbol,
    name: symbol,
    market,
    weight: formatMarketCost(marketValue ?? runningCost ?? quantity, market),
    pnl: formatMarketCurrency(pnl, market),
    risk: '正常',
    role: position.thesis ?? (position.side ? `${position.side} 持仓` : '模拟盘持仓'),
  }
}

async function readSimLedgerSignals(root: string, now: Date): Promise<SignalRow[]> {
  const files = await listSimLedgerFiles(root, 'trade_journal.jsonl')
  const rows = await Promise.all(files.map(async (file) => {
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      return lines.slice(-MAX_SIM_LEDGER_SIGNALS).map((line) => {
        try {
          return parseSimLedgerTrade(JSON.parse(line) as SimLedgerTradeRow, file.market, file.strategy, now)
        } catch {
          return null
        }
      })
    } catch {
      return []
    }
  }))

  return rows
    .flat()
    .filter((row): row is SignalRow => Boolean(row))
    .sort((a, b) => Number.parseInt(a.age, 10) - Number.parseInt(b.age, 10))
    .slice(0, MAX_SIM_LEDGER_SIGNALS)
}

async function readSimLedgerTradeTimeline(root: string): Promise<Map<string, SimLedgerTimelineTrade[]>> {
  const files = await listSimLedgerFiles(root, 'trade_journal.jsonl')
  const timeline = new Map<string, SimLedgerTimelineTrade[]>()

  for (const file of files) {
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          const trade = JSON.parse(line) as SimLedgerTradeRow
          if (trade.capital_layer && String(trade.capital_layer).toLowerCase() !== 'simulated') continue
          if (!trade.timestamp) continue
          const timestampMs = Date.parse(trade.timestamp)
          if (!Number.isFinite(timestampMs)) continue
          const dayKey = compactDate(trade.timestamp)
          if (!dayKey) continue
          const key = performanceTradeKey(dayKey, file.market, file.strategy)
          const rows = timeline.get(key) ?? []
          rows.push({
            market: file.market,
            notional: parseFiniteNumber(trade.notional) ?? 0,
            strategy: file.strategy,
            timestamp: trade.timestamp,
            timestampMs,
          })
          timeline.set(key, rows)
        } catch {
          // Ignore malformed ledger rows; other trades remain usable.
        }
      }
    } catch {
      // Ignore unreadable strategy folders.
    }
  }

  for (const rows of timeline.values()) {
    rows.sort((a, b) => a.timestampMs - b.timestampMs)
  }

  return timeline
}

function parseSimLedgerTrade(row: SimLedgerTradeRow, marketHint: string, strategy: string, now: Date): SignalRow | null {
  if (!row.symbol) return null
  const market = normalizeMarket(marketHint, row.symbol)
  const symbol = normalizeSymbol(row.symbol, market)
  const timestamp = row.timestamp

  return {
    symbol,
    name: symbol,
    market,
    method: `${formatStrategyName(strategy)} · ${row.side === 'sell' ? '卖出' : '买入'}`,
    status: 'executed',
    impact: row.notional === undefined ? '--' : `成交 ${formatCurrencyAmount(row.notional)}`,
    confidence: '已成交',
    age: formatAge(timestamp, now),
    reason: `模拟盘已按 ${formatPrice(row.fill_price)} 成交`,
    next: '进入收益与持仓复盘',
    steps: 6,
    stage: '成交',
    stageTimes: {
      discovered: formatClock(timestamp),
      scored: formatClock(timestamp),
      debated: formatClock(timestamp),
      riskChecked: formatClock(timestamp),
      triggered: formatClock(timestamp),
    },
    stageLatencyMinutes: 0,
    stageEvidence: 'replay',
  }
}

async function readSignalQueue(root: string, now = new Date()): Promise<SignalRow[]> {
  const files: Array<{ bucket: string; path: string }> = []

  for (const bucket of SIGNAL_BUCKETS) {
    const dir = join(root, bucket)
    try {
      const names = await readdir(dir)
      files.push(
        ...names
          .filter((name) => name.endsWith('.json'))
          .sort()
          .slice(0, MAX_SIGNALS_PER_BUCKET)
          .map((name) => ({ bucket, path: join(dir, name) })),
      )
    } catch {
      // Missing buckets are expected while execution writeback is not wired.
    }
  }

  const rows = await Promise.all(files.map((file) => readSignalFile(file.path, file.bucket, now)))
  return rows.filter((row): row is SignalRow => Boolean(row))
}

async function listSimLedgerFiles(root: string, targetName: 'positions.json' | 'trade_journal.jsonl' | 'equity_snapshots.jsonl' | 'daily_mark_to_market.jsonl') {
  const files: Array<{ path: string; market: string; strategy: string }> = []
  try {
    const markets = await readdir(root, { withFileTypes: true })
    for (const market of markets) {
      if (!market.isDirectory()) continue
      const marketRoot = join(root, market.name)
      const strategies = await readdir(marketRoot, { withFileTypes: true })
      for (const strategy of strategies) {
        if (!strategy.isDirectory()) continue
        if (market.name.toLowerCase() === 'ashare' && strategy.name !== 'ashare_sim') continue
        const path = join(marketRoot, strategy.name, targetName)
        if (await fileExists(path)) files.push({ path, market: market.name, strategy: strategy.name })
      }
    }
  } catch {
    return []
  }
  return files
}

async function readSignalFile(path: string, bucket: string, now: Date): Promise<SignalRow | null> {
  try {
    const raw = JSON.parse(await readFile(path, 'utf8')) as SignalFile
    const symbol = raw.ts_code ?? raw.symbol
    if (!symbol) return null
    const status = mapSignalStatus(raw.status ?? bucket, raw)
    const stage = inferSignalStage(raw, bucket)
    const stageTimes = formatStageTimes(raw)

    return {
      symbol,
      name: symbol,
      market: normalizeMarket(raw.market, symbol),
      method: raw.direction ? `${raw.direction}` : '待确认',
      status,
      impact: formatAlphaBps(raw.alpha_bps ?? raw.expected_alpha_bps),
      confidence: raw.confidence ? String(raw.confidence) : '--',
      age: formatAge(raw.discovered_at ?? raw.created_at ?? raw.timestamp, now),
      reason: raw.reason ?? formatRiskReason(raw) ?? '等待下一次确认',
      next: mapSignalNext(bucket),
      steps: stageToSteps(stage),
      stage,
      stageTimes,
      stageLatencyMinutes: calculateStageLatency(raw),
      stageEvidence: getStageEvidence(stageTimes, raw),
    }
  } catch {
    return null
  }
}

async function readJson(path: string) {
  return JSON.parse(await readFile(path, 'utf8')) as unknown
}

function domainHealth(status: ApiStatus, updatedAt: string, message?: string) {
  return message ? { status, updatedAt, message } : { status, updatedAt }
}

async function fileExists(path: string) {
  try {
    await access(path)
    return true
  } catch {
    return false
  }
}

async function directoryHasJson(path: string) {
  try {
    const names = await readdir(path)
    return names.some((name) => name.endsWith('.json'))
  } catch {
    return false
  }
}

function inferMarket(symbol: string): Market {
  if (symbol.endsWith('.HK')) return 'HK'
  if (symbol.endsWith('.SH') || symbol.endsWith('.SZ')) return 'A-share'
  if (symbol.endsWith('.US')) return 'US'
  if (/\.(CFFEX|SHFE|DCE|CZCE|INE|GFEX)$/i.test(symbol)) return 'CNFutures'
  if (/^[A-Z]{2,12}USDT$/.test(symbol) || symbol.includes('-USD') || symbol.includes('PERP')) return 'Crypto'
  if (symbol.startsWith('PM-') || /^\d{5,8}$/.test(symbol)) return 'PM'
  return 'All Markets'
}

function normalizeMarket(market: string | undefined, symbol: string): Market {
  const value = market?.toLowerCase()
  if (value === 'hk') return 'HK'
  if (value === 'a-share' || value === 'ashare' || value === 'a_share' || value === 'cn') return 'A-share'
  if (value === 'us') return 'US'
  if (value === 'crypto') return 'Crypto'
  if (value === 'pm' || value === 'prediction' || value === 'prediction-market') return 'PM'
  if (value === 'cn_futures' || value === 'cnfutures' || value === 'futures' || value === 'china-futures' || value === 'china_futures') return 'CNFutures'
  return inferMarket(symbol)
}

function normalizeSymbol(symbol: string, market: Market) {
  if (market === 'Crypto' && symbol.endsWith('USDT')) return symbol.replace(/USDT$/, '-USD')
  if (market === 'PM' && !symbol.startsWith('PM-')) return `PM-${symbol}`
  return symbol
}

function formatStrategyName(value: string) {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(' ')
}

function mapSignalStatus(status: string, signal?: SignalFile): SignalStatus {
  if (signal?.risk_check?.passed === false) return 'blocked'
  if (status === 'filled' || status === 'executed') return 'executed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'expired' || status === 'failed') return 'missed'
  if (status === 'partial') return 'blocked'
  return 'pending'
}

function mapSignalNext(bucket: string) {
  if (bucket === 'pending') return '等待触发条件'
  if (bucket === 'claimed') return '等待执行确认'
  if (bucket === 'running') return '执行中，等待回执'
  if (bucket === 'filled') return '已进入收益记录'
  return '进入复盘'
}

function inferSignalStage(signal: SignalFile, bucket: string): SignalRow['stage'] {
  const status = signal.status ?? bucket
  if (signal.risk_check?.passed === false || status === 'partial') return '拒绝' as SignalRow['stage']
  if (status === 'failed' || status === 'rejected') return '拒绝' as SignalRow['stage']
  if (status === 'expired' || status === 'cancelled') return '错过' as SignalRow['stage']
  if (status === 'filled' || status === 'executed' || signal.triggered_at || signal.trigger?.triggered_at || signal.fill?.filled_at || signal.simulated_fill?.filled_at || signal.filled_at) return '成交' as SignalRow['stage']
  if (status === 'claimed' || status === 'running') return '待执行' as SignalRow['stage']
  if (signal.risk_checked_at || signal.risk_check) return '风控' as SignalRow['stage']
  if (signal.debated_at || signal.scored_at) return '评分' as SignalRow['stage']
  return '发现' as SignalRow['stage']
}

function formatStageTimes(signal: SignalFile): SignalRow['stageTimes'] {
  const triggeredAt = signal.triggered_at ?? signal.trigger?.triggered_at ?? signal.fill?.filled_at ?? signal.simulated_fill?.filled_at ?? signal.filled_at
  const discoveredAt = signal.discovered_at ?? signal.created_at ?? signal.timestamp
  const riskCheckedAt = signal.risk_checked_at ?? (signal.risk_check ? signal.updated_at ?? signal.timestamp : undefined)

  return {
    discovered: formatClock(discoveredAt),
    scored: formatClock(signal.scored_at),
    debated: formatClock(signal.debated_at),
    riskChecked: formatClock(riskCheckedAt),
    triggered: formatClock(triggeredAt),
  }
}

function calculateStageLatency(signal: SignalFile) {
  const start = Date.parse(signal.discovered_at ?? signal.created_at ?? signal.timestamp ?? '')
  const end = Date.parse(signal.triggered_at ?? signal.trigger?.triggered_at ?? signal.fill?.filled_at ?? signal.simulated_fill?.filled_at ?? signal.filled_at ?? signal.risk_checked_at ?? signal.updated_at ?? signal.scored_at ?? '')
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return undefined
  return Math.round((end - start) / 60000)
}

function stageToSteps(stage: SignalRow['stage']) {
  if (stage === '成交' || stage === '错过') return 6
  if (stage === '待执行') return 5
  if (stage === '风控' || stage === '拒绝') return 4
  if (stage === '评分') return 3
  return 1
}

function getStageEvidence(stageTimes: SignalRow['stageTimes'], signal: SignalFile): SignalRow['stageEvidence'] {
  const values = Object.values(stageTimes ?? {}).filter(Boolean)
  if (values.length >= 3 && new Set(values).size > 1) return 'full'
  if (values.length >= 2 || signal.risk_check || signal.trigger || signal.fill || signal.simulated_fill) return 'partial'
  return 'partial'
}

function buildFunnelEvents(signals: SignalRow[]): FunnelEvent[] {
  return signals.flatMap((signal, index) => {
    const source = signal.stageEvidence === 'replay' ? 'sim_ledger' : 'signal_queue'
    const rank = eventStageRank(signal)
    const baseId = `${source}-${signal.symbol}-${index}`
    const events: FunnelEvent[] = [
      {
        id: `${baseId}-discover`,
        symbol: signal.symbol,
        market: signal.market,
        stage: '发现',
        status: '进入',
        label: '发现机会',
        at: signal.stageTimes?.discovered,
        source,
        reason: signal.reason,
      },
    ]

    if (rank >= 1 || signal.stageTimes?.scored || signal.stageTimes?.debated) {
      events.push({
        id: `${baseId}-research`,
        symbol: signal.symbol,
        market: signal.market,
        stage: '研判',
        status: '通过',
        label: '形成判断',
        at: signal.stageTimes?.debated ?? signal.stageTimes?.scored,
        source,
        reason: signal.method,
      })
    }

    if (rank >= 2 || signal.status === 'blocked') {
      events.push({
        id: `${baseId}-risk`,
        symbol: signal.symbol,
        market: signal.market,
        stage: '风控',
        status: signal.status === 'blocked' ? '拦截' : '通过',
        label: signal.status === 'blocked' ? '风险拦截' : '风控通过',
        at: signal.stageTimes?.riskChecked,
        source,
        reason: signal.reason,
      })
    }

    if (rank >= 3 && signal.status !== 'blocked') {
      events.push({
        id: `${baseId}-queue`,
        symbol: signal.symbol,
        market: signal.market,
        stage: '队列',
        status: signal.status === 'pending' ? '等待' : '通过',
        label: signal.status === 'pending' ? '等待触发' : '进入结果',
        at: signal.stageTimes?.triggered,
        source,
        reason: signal.next,
      })
    }

    if (rank >= 4 || signal.status !== 'pending') {
      events.push({
        id: `${baseId}-result`,
        symbol: signal.symbol,
        market: signal.market,
        stage: '结果',
        status: eventResultStatus(signal),
        label: eventResultLabel(signal),
        at: signal.stageTimes?.triggered,
        source,
        reason: signal.impact,
      })
    }

    return events
  })
}

function eventStageRank(signal: SignalRow) {
  if (signal.stage === '成交' || signal.stage === '错过') return 4
  if (signal.stage === '待执行') return 3
  if (signal.stage === '风控' || signal.stage === '拒绝') return 2
  if (signal.stage === '评分') return 1
  if (signal.steps >= 6) return 4
  if (signal.steps >= 5) return 3
  if (signal.steps >= 4) return 2
  if (signal.steps >= 2) return 1
  return 0
}

function eventResultStatus(signal: SignalRow): FunnelEventStatus {
  if (signal.status === 'executed') return '成交'
  if (signal.status === 'missed') return '机会'
  if (signal.status === 'blocked') return '拦截'
  if (signal.status === 'cancelled') return '复盘'
  return '等待'
}

function eventResultLabel(signal: SignalRow) {
  if (signal.status === 'executed') return '已兑现'
  if (signal.status === 'missed') return '继续观察'
  if (signal.status === 'blocked') return '风险挡住'
  if (signal.status === 'cancelled') return '已取消'
  return '等待结果'
}

function formatRiskReason(signal: SignalFile) {
  if (signal.risk_check?.passed === false) {
    const checks = signal.risk_check.checks?.filter(Boolean).slice(0, 2).join('、')
    return checks ? `风控未通过：${checks}` : '风控未通过'
  }
  return undefined
}

function formatClock(value?: string) {
  if (!value) return undefined
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return undefined
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(date)
}

function formatAge(value: string | undefined, now: Date) {
  if (!value) return '--'
  const created = Date.parse(value)
  if (!Number.isFinite(created)) return '--'
  const minutes = Math.max(0, Math.round((now.getTime() - created) / 60000))
  if (minutes < 60) return `${minutes}分钟`
  return `${Math.round(minutes / 60)}小时`
}

function formatAlphaBps(value: number | undefined) {
  if (value === undefined) return '--'
  const sign = value >= 0 ? '+' : ''
  return `${sign}${Number(value.toFixed(1))} bps`
}

function firstNumber(...values: Array<number | undefined>) {
  return values.find((value): value is number => typeof value === 'number' && Number.isFinite(value))
}

function firstNonEmpty<T>(...values: T[][]) {
  return values.find((value) => value.length > 0) ?? []
}

function formatReviewDay(value?: string) {
  if (!value) return undefined
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (match) return `${Number(match[2])}月${Number(match[3])}日`
  const compactMatch = /^(\d{4})(\d{2})(\d{2})$/.exec(value)
  if (compactMatch) return `${Number(compactMatch[2])}月${Number(compactMatch[3])}日`
  return value
}

function formatTimelineLabel(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return formatReviewDay(value) ?? value
  const parts = new Intl.DateTimeFormat('en-US', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: 'Asia/Shanghai',
  }).formatToParts(date)
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('month')}月${part('day')}日 ${part('hour')}:${part('minute')}`
}

function compactDate(value?: string) {
  const digits = String(value ?? '').replace(/\D/g, '')
  return digits.length >= 8 ? digits.slice(0, 8) : undefined
}

function formatDateForSnapshot(timestampMs: number) {
  return new Date(timestampMs).toISOString().slice(0, 10)
}

function parseFiniteNumber(value: number | string | undefined) {
  const number = typeof value === 'string' ? Number.parseFloat(value) : value
  return typeof number === 'number' && Number.isFinite(number) ? number : undefined
}

function parseNullableNumber(value: unknown) {
  if (value === null || value === undefined || value === '') return null
  return parseFiniteNumber(value as number | string | undefined) ?? null
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function optionalString(value: unknown) {
  return value === undefined || value === null || value === '' ? undefined : String(value)
}

function firstParsedNumber(...values: Array<number | string | undefined>) {
  return values.map(parseFiniteNumber).find((value): value is number => value !== undefined)
}

function parseSnapshotTimestamp(value: string) {
  const direct = Date.parse(value)
  if (Number.isFinite(direct)) return direct
  const compact = /^(\d{4})(\d{2})(\d{2})(?:[T_ -]?(\d{2})(\d{2})(\d{2})?)?/.exec(value)
  if (!compact) return Number.NaN
  const [, year, month, day, hour = '00', minute = '00', second = '00'] = compact
  return Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}+08:00`)
}

function weightedAverage(leftValue: number, leftWeight: number, rightValue: number, rightWeight: number) {
  const totalWeight = Math.max(0, leftWeight) + Math.max(0, rightWeight)
  if (totalWeight <= 0) return rightValue
  return ((leftValue * Math.max(0, leftWeight)) + (rightValue * Math.max(0, rightWeight))) / totalWeight
}

function roundMetric(value: number) {
  return Number(value.toFixed(2))
}

function roundMoney(value: number) {
  return Number(value.toFixed(2))
}

function normalizeCapitalLayer(row: StylePerformanceRow | EquitySnapshotRow) {
  return String(row.capital_layer ?? row.account_type ?? 'simulated').toLowerCase()
}

function performanceTradeKey(dayKey: string, market: string | undefined, style: string | undefined) {
  return `${dayKey}:${normalizeMarketKey(market)}:${normalizeStyleKey(style)}`
}

function normalizeMarketKey(value: string | undefined) {
  const raw = String(value ?? '').toLowerCase()
  if (raw === 'a-share' || raw === 'a_share') return 'ashare'
  if (raw === 'prediction' || raw === 'prediction-market') return 'pm'
  return raw.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
}

function normalizeStyleKey(value: string | undefined) {
  return String(value ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
}

function formatCurrency(value: number) {
  const sign = value >= 0 ? '+' : '-'
  return `${sign}$${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function formatCurrencyAmount(value: number) {
  return `$${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function formatCost(value?: number) {
  if (value === undefined) return '--'
  return `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function formatMarketCost(value: number | undefined, market: HoldingRow['market']) {
  if (value === undefined) return '--'
  return formatMarketCurrencyAmount(value, market)
}

function formatMarketCurrency(value: number, market: HoldingRow['market']) {
  if (market === 'A-share') return formatCny(value, true)
  return formatCurrency(value)
}

function formatMarketCurrencyAmount(value: number, market: HoldingRow['market']) {
  if (market === 'A-share') return formatCny(value, false)
  return `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function formatCny(value: number, signed: boolean) {
  const sign = signed && value > 0 ? '+' : ''
  return `${sign}${new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    maximumFractionDigits: 0,
  }).format(value)}`
}

function formatPrice(value?: number) {
  if (value === undefined) return '账本价格'
  return value.toLocaleString('en-US', { maximumFractionDigits: value < 10 ? 4 : 2 })
}
