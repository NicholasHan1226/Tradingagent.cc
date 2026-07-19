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

export type PaperDayRunSummary = {
  environment: 'local_candidate'
  productionVerified: false
  contractId: 'tradingagent.paper_day_loop.v1'
  runId: string
  tradeDate: string
  status: 'incomplete' | 'incomplete_with_blocks' | 'completed' | 'completed_with_blocks'
  currentStage?: string
  completedStageCount: number
  totalStageCount: 9
  dataEvidenceState: 'ready' | 'degraded' | 'unavailable'
  simulationExecutionState: 'eligible' | 'blocked' | 'no_orders'
  candidateCount: number
  decisionCount: number
  simulatedOrderCount: number
  simulatedFillCount: number
  noTradeReasons: string[]
  riskBlocks: string[]
  championManifestSha256: string
  llmEvidenceState: 'evidence_only' | 'unavailable'
  source: 'shared/runtime/run_bundles/latest.json'
}

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
  /** Nullable slot for per-market capital authority identity. null when source does not yet expose it; never invent values. */
  capitalAuthorityId?: string | null
  /** Nullable slot for authority generation number. null when source does not yet expose it. */
  authorityGeneration?: number | null
  /** Current market-capital execution lineage. Never inferred from a style ledger. */
  executionLineageId?: string | null
  /** Nullable slot for maturity label. null when source does not yet expose it. */
  maturity?: string | null
  /** CNFutures-only maturity evidence; never attached to another market. */
  cnFuturesMaturityEvidence?: CNFuturesMarketMaturityProjection
  /** Current-market capital deployment only; never summed across style shadow ledgers. */
  capitalUtilizationPct?: number
  deployedCapitalCny?: number
  availableToReserveCny?: number
  riskUsedCny?: number
  riskLimitCny?: number
  undeployedReasons?: MarketUndeployedReason[]
}

export type MarketUndeployedReason = {
  code: string
  amountCny?: number
  details?: string
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
  capitalAuthorityId: 'ashare-capital-v1'
  authorityGeneration: number
  executionLineageId: string
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

export type AShareProjectionAuthority = {
  capitalAuthorityId: string
  authorityGeneration: number
  executionLineageId: string
}

export type CNFuturesProjectionAuthority = {
  capitalAuthorityId: 'cn-futures-capital-v1'
  authorityGeneration: 1
  executionLineageId: string
}

export type AShareSampleKpiProjection = {
  source: 'sample_journal_kpi'
  generatedAt: string
  tradeDate: string
  authorityScope: AShareProjectionAuthority
  journalEventCount: number
  candidateCount: number
  predictionCount: number
  observationCounterfactualCount: number
  explorationFillCount: number
  exploitationFillCount: number
  completedRoundTripCount: number
  riskRejectCount: number
  readyForwardLabelCount: number
  pendingForwardLabelCount: number
  styles: AShareStyleKpiProjection[]
  promotionEvidenceReady: boolean
  automaticPromotionEnabled: false
  automaticRiskExpansionEnabled: false
  realTradingEnabled: false
}

export type AShareStyleKpiProjection = {
  styleId: string
  candidateCount: number
  predictionCount: number
  observationCounterfactualCount: number
  explorationFillCount: number
  exploitationFillCount: number
  completedRoundTripCount: number
  readyForwardLabelCount: number
  pendingForwardLabelCount: number
  riskRejectCount: number
  winRate: number | null
  expectancyCny: number | null
  postCostPnlCny: number | null
  maxDrawdownCny: number | null
  rejectionReasons: Array<{ reason: string; count: number }>
}

export type AShareMarketMaturityProjection = {
  source: 'sample_journal_kpi'
  generatedAt: string
  tradeDate: string
  authorityScope: AShareProjectionAuthority
  stage: string
  totalTradingDays: number
  checkpointDue?: number
  promotionEvidenceReady: boolean
  liveTransitionAuthorized: false
  automaticPromotionEnabled: false
  automaticRiskExpansionEnabled: false
  realTradingEnabled: false
}

export type CNFuturesMaturitySampleCounts = {
  validSampleCount: number
  observationCounterfactualCount: number
  counterfactualOnlyCount: number
  executionEligibleSampleCount: number
  completedRoundTripCount: number
  forwardLabelCount: number
  pendingForwardLabelCount: number
  riskRejectCount: number
}

export type CNFuturesMaturityCoverage = {
  products: string[]
  productCount: number
  volatilityRegimes: string[]
  volatilityRegimeCount: number
  nightSessionSampleCount: number
  rolloverSampleCount: number
  marginEvidenceSampleCount: number
  feeEvidenceSampleCount: number
  slippageEvidenceSampleCount: number
  extremeRiskSampleCount: number
}

export type CNFuturesMaturityPerformance = {
  winRate: number | null
  expectancyCny: number | null
  postCostPnlCny: number | null
  maxDrawdownCny: number | null
  stabilityScore: number | null
}

export type CNFuturesMarketMaturityProjection = {
  source: 'cn_futures_review_journal+sample_kpi'
  generatedAt: string
  tradeDate: string
  freshStartTradeDate: string
  authorityScope: CNFuturesProjectionAuthority
  capitalPoolCny: 50000
  marginUtilizationLimitCny: 25000
  stage: string
  simulationTradingDays: string[]
  totalSimulationTradingDays: number
  sampleCounts: CNFuturesMaturitySampleCounts
  coverage: CNFuturesMaturityCoverage
  performance: CNFuturesMaturityPerformance
  blockingReasons: string[]
  promotionEvidenceReady: boolean
  automaticPromotionEnabled: false
  automaticRiskExpansionEnabled: false
  liveTransitionAuthorized: false
  realTradingEnabled: false
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
  source: 'signal_queue' | 'sim_ledger' | 'opportunity_log' | 'legacy_frozen_opportunity_log'
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
      predictionCount?: number
      explorationFillCount?: number
      exploitationFillCount?: number
      completedRoundTripCount?: number
    }
  }
}
