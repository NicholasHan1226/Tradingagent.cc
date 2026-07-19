import type { ApiStatus, DashboardApiResponse } from './types.ts'
import type { AShareForwardValidation, AShareMarketMaturityProjection, AShareResearchEvidence, AShareSampleKpiProjection, AShareTierSummary, CNFuturesMarketMaturityProjection, FunnelEvent, HoldingRow, MarketPulse, MarketPulseCoverage, MarketPulseCoverageObservation, MarketSummary, PaperDayRunSummary, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard.ts'
import type { DataDomain } from '../types/status.ts'

export const tradingAgentReadModelSources = {
  capitalPlan: 'shared/accounting/position_plan.jsonl',
  positions: 'signals/positions/*.json',
  filledSignals: 'signals/filled/*.json',
  signalQueue: 'signals/{pending,claimed,running,filled,expired,cancelled,failed,partial}',
  opportunityEvents: 'legacy frozen forensic only: shared/review/opportunities/funnel_events.jsonl or shared/logs/opportunities/funnel_events.jsonl',
  review: 'shared/review/daily/daily_brief.jsonl',
  middayReview: 'shared/review/daily/midday_review.jsonl',
  riskLimits: 'shared/risk/risk_limits.yaml',
  strategyAttribution: 'shared/review/attribution/strategy_attribution.jsonl',
  factorAttribution: 'shared/review/attribution/factor_attribution.jsonl',
  strategyVersion: 'shared/review/strategies/strategy_version.jsonl',
  simLedger: 'shared/logs/sim_ledger/*/*/{positions.json,trade_journal.jsonl}',
  localSimLedger: 'shared/logs/capital/ashare/ashare_sim_capital_latest.json -> shared/logs/execution_lineages/{verified execution_lineage_id}/local_sim_trades.jsonl',
  ashareMarketCapital: 'shared/logs/capital/ashare/ashare_sim_capital_latest.json',
  cnFuturesMarketCapital: 'shared/logs/capital/cn_futures/cn_futures_sim_capital_latest.json',
  equitySnapshots: 'shared/review/{portfolio,daily,*}/{equity_snapshots,equity_series}.jsonl and shared/logs/sim_ledger/*/*/{daily_mark_to_market,equity_snapshots}.jsonl',
  performanceTracker: 'shared/review/*/style_performance.jsonl',
  styleComparison: 'shared/review/*/style_comparison.json',
  simMarketHealth: 'shared/runtime_test/sim_market_health_latest.json',
  capitalFlow: 'TradingAgent signal score evidence only; no active legacy SharedSignals endpoint read',
  cnFuturesReview: 'shared/review/data/cn_futures_sim_reviews.jsonl',
  ashareResearchEvidence: 'shared/review/ashare/research_evidence_latest.json',
  ashareSampleKpi: 'shared/review/ashare/projection_current.json -> projection_generations/*/sample_kpi_latest.json',
  ashareMarketMaturity: 'shared/review/ashare/projection_current.json -> projection_generations/*/market_maturity_latest.json',
  cnFuturesMarketMaturity: 'shared/review/cn_futures/market_maturity_latest.json',
  cnFuturesReplay: 'shared/review/cn_futures/replay_latest.json',
  sharedSignalsMarketPulse: 'SharedSignals V1 GET /v1/catalog + POST /v1/query via explicit base URL, catalog version, access policy, and per-market dataset IDs; fail closed with no legacy fallback',
  paperDayRunBundle: 'shared/runtime/run_bundles/latest.json',
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
  marketPulses?: MarketPulse[]
  marketPulseCoverage?: MarketPulseCoverage
  marketPulseCoverageHistory?: MarketPulseCoverageObservation[]
  ashareResearchEvidence?: AShareResearchEvidence
  ashareSampleKpi?: AShareSampleKpiProjection
  ashareMarketMaturity?: AShareMarketMaturityProjection
  cnFuturesMarketMaturity?: CNFuturesMarketMaturityProjection
  /** Compatibility view derived only from ashareSampleKpi; never a legacy file read. */
  ashareForwardValidation?: AShareForwardValidation
  ashareTierSummaries?: AShareTierSummary[]
  paperDayRun?: PaperDayRunSummary
  sourceRefs: typeof tradingAgentReadModelSources
}
