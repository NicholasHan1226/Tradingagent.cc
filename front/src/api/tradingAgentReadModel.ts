import type { ApiStatus, DashboardApiResponse } from './types.ts'
import type { AShareForwardValidation, AShareResearchEvidence, FunnelEvent, HoldingRow, MarketSummary, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard.ts'
import type { DataDomain } from '../types/status.ts'

export const tradingAgentReadModelSources = {
  capitalPlan: 'shared/accounting/position_plan.jsonl',
  positions: 'signals/positions/*.json',
  filledSignals: 'signals/filled/*.json',
  signalQueue: 'signals/{pending,claimed,running,filled,expired,cancelled,failed,partial}',
  opportunityEvents: 'shared/review/opportunities/funnel_events.jsonl or shared/logs/opportunities/funnel_events.jsonl',
  review: 'shared/review/daily/daily_brief.jsonl',
  middayReview: 'shared/review/daily/midday_review.jsonl',
  riskLimits: 'shared/risk/risk_limits.yaml',
  strategyAttribution: 'shared/review/attribution/strategy_attribution.jsonl',
  factorAttribution: 'shared/review/attribution/factor_attribution.jsonl',
  strategyVersion: 'shared/review/strategies/strategy_version.jsonl',
  simLedger: 'shared/logs/sim_ledger/*/*/{positions.json,trade_journal.jsonl}',
  localSimLedger: 'shared/logs/local_sim/local_sim_trades.jsonl',
  equitySnapshots: 'shared/review/{portfolio,daily,*}/{equity_snapshots,equity_series}.jsonl and shared/logs/sim_ledger/*/*/{daily_mark_to_market,equity_snapshots}.jsonl',
  performanceTracker: 'shared/review/*/style_performance.jsonl',
  styleComparison: 'shared/review/*/style_comparison.json',
  simMarketHealth: 'shared/runtime_test/sim_market_health_latest.json',
  capitalFlow: 'SharedSignals /capital_flow via TradingAgent signal scores',
  cnFuturesReview: 'shared/review/data/cn_futures_sim_reviews.jsonl',
  ashareResearchEvidence: 'shared/review/ashare/research_evidence_latest.json',
  ashareForwardValidation: 'shared/review/ashare/forward_validation_latest.json',
  cnFuturesReplay: 'shared/review/cn_futures/replay_latest.json',
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
  portfolio?: PortfolioSummary
  holdings: HoldingRow[]
  signals: SignalRow[]
  funnelEvents: FunnelEvent[]
  marketSummaries?: MarketSummary[]
  ashareResearchEvidence?: AShareResearchEvidence
  ashareForwardValidation?: AShareForwardValidation
  sourceRefs: typeof tradingAgentReadModelSources
}
