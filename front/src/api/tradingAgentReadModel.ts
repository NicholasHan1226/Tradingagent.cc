import type { ApiStatus, DashboardApiResponse } from './types.ts'
import type { HoldingRow, PerformancePoint, SignalRow } from '../types/dashboard.ts'
import type { DataDomain } from '../types/status.ts'

export const tradingAgentReadModelSources = {
  capitalPlan: 'TradingAgent/shared/accounting/position_plan.jsonl',
  positions: 'TradingAgent/signals/positions/*.json',
  filledSignals: 'TradingAgent/signals/filled/*.json',
  signalQueue: 'TradingAgent/signals/{pending,filled,expired,cancelled,failed,partial}',
  review: 'TradingAgent/shared/review/daily/daily_brief.jsonl',
  middayReview: 'TradingAgent/shared/review/daily/midday_review.jsonl',
  riskLimits: 'TradingAgent/shared/risk/risk_limits.yaml',
  strategyAttribution: 'TradingAgent/shared/review/attribution/strategy_attribution.jsonl',
  factorAttribution: 'TradingAgent/shared/review/attribution/factor_attribution.jsonl',
  strategyVersion: 'TradingAgent/shared/review/strategies/strategy_version.jsonl',
} as const

export type TradingAgentReadModelHealth = {
  status: ApiStatus
  updatedAt: string
  message?: string
}

export type TradingAgentReadModelSnapshot = {
  mode: DashboardApiResponse['mode']
  generatedAt: string
  domains: Record<DataDomain, TradingAgentReadModelHealth>
  performance: PerformancePoint[]
  holdings: HoldingRow[]
  signals: SignalRow[]
  sourceRefs: typeof tradingAgentReadModelSources
}
