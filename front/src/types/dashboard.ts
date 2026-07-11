export type Market = 'All Markets' | 'A-share' | 'US' | 'Crypto' | 'HK' | 'PM' | 'CNFutures'
export type Page = '总览' | '收益' | '过程' | '持仓' | '风险' | '复盘'
export type LegacyPage = '主页' | '机会' | '决策'
export type AccountMode = 'simulated' | 'live'
export type SignalStatus = 'executed' | 'missed' | 'blocked' | 'pending' | 'cancelled'
export type BookTone = 'cyan' | 'red' | 'amber' | 'muted'

export type PageMeta = {
  title: string
  copy: string
  mode: string
}

export type PerformancePoint = {
  day: string
  timestamp?: string
  simulated: number
  target: number
  benchmark: number
  opportunity: number
  quality?: 'normal' | 'outlier'
  qualityReason?: string
}

export type PerformanceRange = 'today' | '7d' | '30d' | 'all'

export type PortfolioSummary = {
  pnlAmount: number
  returnPct: number
  capitalBase: number
  targetPct: number
  maxDrawdownPct: number
  tradeCount: number
  pointCount: number
  source: string
  pnlSource?: string
  pnlCurrency?: 'USD' | 'CNY'
  realizedPnl?: number
  unrealizedPnl?: number
  ashareAccount?: AShareAccountSummary
  updatedAt: string
}

export type MarketSummaryStatus = 'ready' | 'partial' | 'empty' | 'paused'
export type MarketRuntimeState = 'normal' | 'strategy_wait' | 'needs_attention' | 'empty'

export type MarketSummary = {
  market: Market
  status: MarketSummaryStatus
  runtimeState?: MarketRuntimeState
  executionFault?: boolean
  runtimeReason?: string
  noTradeEvidence?: AShareNoTradeEvidence
  cnFuturesReplayEvidence?: CNFuturesReplayEvidence
  holdingCount: number
  signalCount: number
  tradeCount: number
  styleCount: number
  activeStyleCount?: number
  degradedStyleCount?: number
  pausedStyleCount?: number
  filledCount?: number
  errorCount?: number
  capitalBase?: number
  pnlAmount?: number
  pnlCurrency?: 'USD' | 'CNY'
  returnPct?: number
  maxDrawdownPct?: number
  realizedPnl?: number
  unrealizedPnl?: number
  latestAt?: string
  source: string
  headline: string
  detail: string
}

export type MarketPulse = {
  market: Exclude<Market, 'All Markets'>
  symbol: string
  lastPrice: number
  changePct?: number
  high?: number
  low?: number
  volume?: number
  updatedAt?: string
  freshness: 'live' | 'stale' | 'degraded'
  points: number[]
  source: string
}

export type MarketPulseCoverageStatus = 'sourced' | 'no_representative' | 'unavailable' | 'degraded'

export type MarketPulseCoverageEntry = {
  market: Exclude<Market, 'All Markets'>
  symbol?: string
  status: MarketPulseCoverageStatus
}

export type MarketPulseCoverage = {
  cacheState: 'fresh' | 'cached'
  entries: MarketPulseCoverageEntry[]
  fetchedAt: string
  requestedCount: number
  sourcedCount: number
  sourceLatencyMs: number
}

export type MarketPulseCoverageObservation = Omit<MarketPulseCoverage, 'cacheState'>

export type CNFuturesReplayEvidence = {
  generatedAt: string
  date: string
  readOnly: boolean
  realTradingEnabled: boolean
  symbolCount: number
  styleCount: number
  windowCount: number
  buyCount: number
  sellCount: number
  holdCount: number
  actionableCount: number
  executableCount: number
  nonExecutableReason?: string
  topReason?: string
  topSymbol?: string
}

export type AShareNoTradeEvidence = {
  category?: string
  action?: string
  evidenceStatus: 'ready' | 'incomplete' | 'no_rows'
  evidenceGaps: string[]
  universeCount?: number
  candidateCount?: number
  orderCount?: number
  riskRejectionCount?: number
  skippedCandidateCount?: number
  executionSkipCount?: number
  candidateTraceCount?: number
  capitalPlanCapacity?: number
  targetPositions?: number
  riskMode?: string
  allowedBuyCount?: number
  accountCashAvailable?: number
  strategyCashAvailable?: number
  accountPositionCount?: number
  strategyPositionCount?: number
  ignoredValidationSampleCount?: number
  strategySampleValidCount?: number
}

export type AShareAccountSummary = {
  cashAvailable: number
  marketValue: number
  accountEquity: number
  accountTotalPnl: number
  accountRealizedPnl?: number
  accountUnrealizedPnl?: number
  accountReturnPct: number
  openPositionCount: number
  totalSampleCount: number
  validationSampleCount: number
  strategySampleValidCount: number
  strategyTotalPnl?: number
  strategyMarketValue?: number
  strategyOpenPositionCount?: number
  source: string
  updatedAt: string
}

export type AShareForwardValidation = {
  generatedAt: string
  date: string
  readOnly: boolean
  realTradingEnabled: boolean
  tradeCount: number
  strategyLabelCount: number
  pendingCount: number
}

export type AShareTierSummary = {
  account: string
  label: string
  capital: number
  totalPnl: number
  returnPct: number
  marketValue?: number
  cashAvailable?: number
  tradeCount: number
  source: string
  updatedAt: string
}

export type ChartEvent = {
  day: string
  title: string
  targetPage: Page
  summary: string
}

export type SignalRow = {
  symbol: string
  name: string
  market: Market
  opportunityId?: string
  marketDataSymbol?: string
  method: string
  strategyName?: string
  signalSource?: string
  queueBucket?: string
  status: SignalStatus
  impact: string
  confidence: string
  age: string
  reason: string
  next: string
  steps: number
  stage?: '发现' | '评分' | '风控' | '待执行' | '成交' | '错过' | '拒绝'
  stageTimes?: Partial<Record<'discovered' | 'scored' | 'debated' | 'riskChecked' | 'triggered', string>>
  stageLatencyMinutes?: number
  stageEvidence?: 'full' | 'partial' | 'replay'
  capitalEvidence?: SignalCapitalEvidence
}

export type SignalCapitalEvidence = {
  score?: number
  netInflow?: number
  mainNetInflow?: number
  largeOrderNetInflow?: number
  superLargeOrderNetInflow?: number
  source: string
}

export type FunnelEventStage = '发现' | '研判' | '风控' | '待确认' | '结果'
export type FunnelEventStatus = '进入' | '通过' | '等待' | '成交' | '机会' | '拦截' | '复盘'

export type FunnelEvent = {
  id: string
  symbol: string
  market: Market
  opportunityId?: string
  sequence?: number
  stage: FunnelEventStage
  status: FunnelEventStatus
  label: string
  at?: string
  source: 'signal_queue' | 'sim_ledger' | 'opportunity_log'
  reason?: string
  latencyMinutes?: number
  terminal?: boolean
}

export type HoldingRow = {
  symbol: string
  name: string
  market: Market
  opportunityId?: string
  marketDataSymbol?: string
  weight: string
  pnl: string
  risk: '正常' | '观察' | '偏高'
  role: string
  quantity?: number
  averagePrice?: number
  markPrice?: number
  costBasis?: number
  marketValue?: number
  dayPnl?: number
  realizedPnl?: number
  unrealizedPnl?: number
  currency?: 'CNY' | 'USD'
  updatedAt?: string
  source?: 'sim_ledger' | 'position_snapshot' | 'legacy_position_ledger'
}

export type DepthRow = {
  label: string
  value: string
  total: string
  tone: BookTone
}

export type AShareResearchEvidence = {
  generatedAt: string
  tradeDate: string
  readOnly: boolean
  realTradingEnabled: boolean
  openingAuction: {
    state: string
    phase: string
    dataMode?: string
    anomalyCount: number
    symbolsWithBars: number
    proxySymbolsWithBars?: number
  }
  closingMomentum: {
    state: string
    candidateCount: number
    symbolsWithBars: number
    candidates: Array<{
      symbol: string
      tailMomentum?: number
      volumeRatio?: number
      labelState?: string
      nextDayOpenReturn?: number | null
      nextDayHighReturn?: number | null
    }>
  }
  reverseRepo: {
    action: string
    amount: number
    lots: number
    annualizedYield: number
    yieldSource?: string
    estimatedInterest: number
  }
  styleEvidence: {
    summary: {
      styles: number
      activeSample?: number
      degraded?: number
      paused?: number
      virtualCapital?: number
      allocatedCapital?: number
      unallocatedCapital?: number
    }
  }
}
