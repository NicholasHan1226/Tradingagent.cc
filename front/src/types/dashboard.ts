export type Market = 'All Markets' | 'A-share' | 'US' | 'Crypto' | 'HK' | 'PM'
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
  simulated: number
  target: number
  benchmark: number
  opportunity: number
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
  status: SignalStatus
  impact: string
  confidence: string
  age: string
  reason: string
  next: string
  steps: number
  stage?: '发现机会' | '形成信号' | '交易条件' | '风险筛选' | '执行确认'
  stageTimes?: Partial<Record<'discovered' | 'scored' | 'debated' | 'riskChecked' | 'triggered', string>>
  stageLatencyMinutes?: number
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
