export type Market = 'All Markets' | 'A-share' | 'US' | 'Crypto' | 'HK' | 'PM' | 'CNFutures'
export type Page = '主页' | '收益' | '机会' | '持仓' | '决策' | '风险' | '复盘'
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

export type MarketSummary = {
  market: Market
  status: MarketSummaryStatus
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

export type AShareAccountSummary = {
  cashAvailable: number
  marketValue: number
  accountEquity: number
  accountTotalPnl: number
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
  method: string
  strategyName?: string
  signalSource?: string
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
}

export type FunnelEventStage = '发现' | '研判' | '风控' | '队列' | '结果'
export type FunnelEventStatus = '进入' | '通过' | '等待' | '成交' | '机会' | '拦截' | '复盘'

export type FunnelEvent = {
  id: string
  symbol: string
  market: Market
  stage: FunnelEventStage
  status: FunnelEventStatus
  label: string
  at?: string
  source: 'signal_queue' | 'sim_ledger'
  reason?: string
}

export type HoldingRow = {
  symbol: string
  name: string
  market: Market
  weight: string
  pnl: string
  risk: '正常' | '观察' | '偏高'
  role: string
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
