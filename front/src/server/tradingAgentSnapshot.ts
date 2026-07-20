import { access, lstat, readFile, readdir } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { basename, dirname, join } from 'node:path'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel.ts'
import type { ApiStatus } from '../api/types.ts'
import type { AShareForwardValidation, AShareMarketMaturityProjection, AShareNoTradeEvidence, AShareProjectionAuthority, AShareResearchEvidence, AShareSampleKpiProjection, AShareTierSummary, CNFuturesMarketMaturityProjection, CNFuturesProjectionAuthority, CNFuturesReplayEvidence, FunnelEvent, FunnelEventStatus, HoldingRow, Market, MarketSummary, PaperDayRunSummary, PerformancePoint, PortfolioSummary, SignalCapitalEvidence, SignalRow, SignalStatus } from '../types/dashboard.ts'
import { readSharedSignalsMarketPulses } from './sharedSignalsMarketPulse.ts'

type SnapshotOptions = {
  workspaceRoot: string
  signalQueueDir?: string
  now?: Date
}

type PositionRow = {
  account?: string
  account_id?: string
  account_scope?: string
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
  opportunity_id?: string | number
  opportunityId?: string | number
  signal_id?: string | number
  trace_id?: string | number
  order_id?: string | number
  market_data_symbol?: string
  marketDataSymbol?: string
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
  opportunity_id?: string | number
  opportunityId?: string | number
  signal_id?: string | number
  trace_id?: string | number
  order_id?: string | number
  market_data_symbol?: string
  marketDataSymbol?: string
}

type CNFuturesPositionsFile = {
  positions?: CNFuturesPositionRow[]
}

type SimLedgerPosition = {
  avg_cost?: number
  market_id?: string
  outcome?: string
  quantity?: number
  realized_pnl?: number
  unrealized_pnl?: number
  opportunity_id?: string | number
  opportunityId?: string | number
  signal_id?: string | number
  trace_id?: string | number
  order_id?: string | number
  market_data_symbol?: string
  marketDataSymbol?: string
}

type SimLedgerPositionsFile = {
  account?: string
  account_id?: string
  account_scope?: string
  cash?: number
  positions?: Record<string, SimLedgerPosition>
}

type LocalSimTradeRow = {
  ashare_session_valid?: boolean
  candidate_pool_layer?: string
  created_at?: string
  execution_source?: string
  filled_at?: string
  market?: string
  side?: string
  status?: string
  timestamp?: string
  capital_authority_id?: string
  authority_generation?: number | string
  execution_lineage_id?: string
}

type LocalSimAccountPnl = {
  capital_authority_id?: string
  authority_generation?: number | string
  execution_lineage_id?: string
  cash_available?: number | string
  market_value?: number | string
  realized_pnl?: number | string
  unrealized_pnl?: number | string
  total_pnl?: number | string
  total_trades?: number | string
  positions?: Record<string, unknown>
}

type MarketCapitalProjection = {
  market: 'A-share' | 'CNFutures'
  authorityId: 'ashare-capital-v1' | 'cn-futures-capital-v1'
  authorityGeneration: number
  executionLineageId: string
  initialEquityCny: 50000
  equityCny: number
  cashBalanceCny: number
  positionsMarketValueCny: number
  marginUsedCny: number
  realizedPnlCny: number
  unrealizedPnlCny: number
  updatedAt: string
  openPositionCount: number
  positionsQuantityByRiskUnit: Record<string, number>
  deployedCapitalCny: number
  availableToReserveCny: number
  capitalUtilizationPct: number
  riskUsedCny: number
  riskLimitCny: number
  source: string
}

type ASharePositionAuthorityState = 'ready' | 'empty' | 'unavailable' | 'not_applicable'

type ASharePositionAuthorityRead = {
  holdings: HoldingRow[]
  state: ASharePositionAuthorityState
}

type AShareNoTradeExplanation = {
  category?: string
  action?: string
  counts?: Record<string, unknown>
  candidate_decision_trace?: unknown[]
  capital_plan_decision?: Record<string, unknown>
  portfolio_decision?: Record<string, unknown>
}

type AShareNoTradeLogRow = {
  date?: string
  generated_at?: string
  no_trade_explanation?: AShareNoTradeExplanation
}

type CNFuturesReviewRow = {
  error_count?: number | string
  filled_count?: number | string
  generated_at?: string
  hold_count?: number | string
  record_count?: number | string
  signal_count?: number | string
  state?: string
}

type CNFuturesReplayPayload = {
  action_counts?: Record<string, number | string>
  actionable_examples?: unknown[]
  date?: string
  generated_at?: string
  read_only?: boolean
  real_trading_enabled?: boolean
  style_count?: number | string
  style_summary?: Record<string, unknown>
  symbol_count?: number | string
  window_count?: number | string
}

type SimMarketHealthCheck = {
  name?: string
  status?: 'pass' | 'warn' | 'fail' | string
  summary?: string
  details?: {
    market?: string
    diagnostic_class?: string
    execution_fault?: boolean
    fail_reasons?: string[]
    warn_reasons?: string[]
  }
}

type SimMarketHealthSummary = {
  status: 'pass' | 'warn' | 'fail' | string
  summary?: string
  diagnosticClass?: string
  executionFault?: boolean
  reasons: string[]
}

type SignalFile = {
  id?: string | number
  signal_id?: string | number
  opportunity_id?: string | number
  trace_id?: string | number
  order_id?: string | number
  card_id?: string | number
  market_data_symbol?: string
  marketDataSymbol?: string
  metadata?: Record<string, unknown>
  ts_code?: string
  symbol?: string
  market?: string
  candidate_pool_layer?: string
  execution_source?: string
  direction?: string
  side?: string
  status?: string
  confidence?: string | number
  score?: Record<string, unknown>
  scores?: Record<string, unknown>
  dimension_scores?: Record<string, unknown>
  factor_scores?: Record<string, unknown>
  capital_score?: number | string
  moneyflow_score?: number | string
  net_mf_amount?: number | string
  main_net_inflow?: number | string
  large_order_net_inflow?: number | string
  super_large_order_net_inflow?: number | string
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

type OpportunityFunnelEventRow = {
  at?: string
  card_id?: string | number
  created_at?: string
  event_id?: string | number
  id?: string | number
  label?: string
  latency_minutes?: number | string
  latencyMinutes?: number | string
  market?: string
  metadata?: Record<string, unknown>
  opportunity_id?: string | number
  opportunityId?: string | number
  order_id?: string | number
  reason?: string
  sequence?: number | string
  signal_id?: string | number
  source?: string
  stage?: string
  status?: string
  symbol?: string
  terminal?: boolean | string
  timestamp?: string
  trace_id?: string | number
  ts?: string
  ts_code?: string
  updated_at?: string
}

type SimLedgerTradeRow = {
  account_type?: string
  capital_layer?: string
  card_id?: string
  dashboard_excluded?: boolean | string
  exclude_from_dashboard?: boolean | string
  excluded_from_dashboard?: boolean | string
  fill_price?: number
  fill_qty?: number
  market_id?: string
  metadata?: Record<string, unknown>
  notional?: number
  order_id?: string
  outcome?: string
  opportunity_id?: string
  realized_pnl?: number
  reason?: string
  run_context?: string
  run_mode?: string
  run_source?: string
  sample_type?: string
  side?: string
  signal_id?: string
  signal_source?: string
  source?: string
  strategy_name?: string
  trace_id?: string
  conviction?: number | string
  symbol?: string
  timestamp?: string
}

type MarketPerformanceSummary = {
  capitalBase?: number
  currency: NonNullable<MarketSummary['pnlCurrency']>
  latestAt?: string
  maxDrawdown: number
  pnl: number
  realizedPnl: number
  trades: number
  unrealizedPnl: number
}

function marketCapitalPerformanceSummary(
  capital: MarketCapitalProjection,
): MarketPerformanceSummary {
  const pnl = roundMoney(capital.equityCny - capital.initialEquityCny)
  const currentDrawdownPct = capital.initialEquityCny > 0
    ? Math.max(0, ((capital.initialEquityCny - capital.equityCny) / capital.initialEquityCny) * 100)
    : 0
  return {
    capitalBase: capital.initialEquityCny,
    currency: 'CNY',
    latestAt: capital.updatedAt,
    maxDrawdown: roundMetric(currentDrawdownPct),
    pnl,
    realizedPnl: capital.realizedPnlCny,
    trades: 0,
    unrealizedPnl: capital.unrealizedPnlCny,
  }
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
  account?: string
  account_id?: string
  account_scope?: string
  dashboard_excluded?: boolean | string
  timestamp?: string
  ts?: string
  as_of?: string
  generated_at?: string
  updated_at?: string
  date?: string
  trade_date?: string
  equity?: number | string
  equity_cny?: number | string
  total_equity?: number | string
  total_equity_cny?: number | string
  nav?: number | string
  net_value?: number | string
  account_value?: number | string
  portfolio_value?: number | string
  cash_cny?: number | string
  capital_base?: number | string
  capital_base_cny?: number | string
  initial_equity?: number | string
  starting_equity?: number | string
  start_equity?: number | string
  principal?: number | string
  pnl?: number | string
  total_pnl?: number | string
  net_pnl?: number | string
  pnl_cny?: number | string
  total_pnl_cny?: number | string
  net_pnl_cny?: number | string
  realized_pnl?: number | string
  realized_pnl_cny?: number | string
  unrealized_pnl?: number | string
  unrealized_pnl_cny?: number | string
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
  exclude_from_dashboard?: boolean | string
  excluded_from_dashboard?: boolean | string
  metadata?: Record<string, unknown>
  pnl_source?: string
  run_context?: string
  run_mode?: string
  run_source?: string
  sample_type?: string
  source?: string
  currency?: string
  display_currency?: string
  fx_to_cny?: number | string
  capital_layer?: string
  account_type?: string
  real_execution?: boolean
}

type EquitySnapshotRecord = EquitySnapshotRow & {
  sourcePath: string
}

type ParsedEquitySnapshot = {
  accountScope: string
  benchmarkPct: number
  capitalBase: number
  currency: NonNullable<MarketSummary['pnlCurrency']>
  dayKey: string
  isSimLedgerSnapshot: boolean
  markets: Set<Market>
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
const MAX_OPPORTUNITY_FUNNEL_EVENTS = 300
const DEFAULT_TARGET_RETURN_PCT = 8
const DEFAULT_SIM_CAPITAL_CNY = 50_000
const SIM_LEDGER_EQUITY_BUCKET_MS = 5 * 60 * 1000
const MAX_EQUITY_PERFORMANCE_POINTS = 360
const MAX_SIM_MARKET_HEALTH_AGE_MS = 30 * 60 * 1000
const MAX_CAPITAL_AUTHORITY_AGE_MS = 36 * 60 * 60 * 1000
const MAX_POSITION_AUTHORITY_AGE_MS = 36 * 60 * 60 * 1000
const DASHBOARD_MARKETS: Market[] = ['A-share', 'CNFutures', 'Crypto']

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
  const reviewRoot = join(projectRoot, 'shared/review')
  const simLedgerRoot = join(projectRoot, 'shared/logs/sim_ledger')
  const nonAuthoritativeHoldings = await readPositionSnapshots(positionsPath)
  const planHoldings = await readPositionPlan(positionPlanPath)
  const simLedgerHoldings = await readSimLedgerHoldings(simLedgerRoot)
  const queueSignals = await readSignalQueue(queueRoot, now)
  const simLedgerSignals = await readSimLedgerSignals(simLedgerRoot, now)
  const signals = mergeSignals(queueSignals, simLedgerSignals)
  const opportunityFunnelEvents = await readOpportunityFunnelEvents(projectRoot)
  const funnelEvents = mergeFunnelEvents(opportunityFunnelEvents, buildFunnelEvents([...queueSignals, ...simLedgerSignals]))
  const reviewPerformance = firstNonEmpty(await readPerformanceSeries(reviewPath), await readPerformanceSeries(reviewFallbackPath))
  const ashareMarketCapital = await readMarketCapitalProjection(
    toProjectPath(projectRoot, tradingAgentReadModelSources.ashareMarketCapital),
    'A-share',
    now,
  )
  const cnFuturesMarketCapital = await readMarketCapitalProjection(
    toProjectPath(projectRoot, tradingAgentReadModelSources.cnFuturesMarketCapital),
    'CNFutures',
    now,
  )
  const asharePositionAuthority = await readAuthoritativeASharePositions(projectRoot, ashareMarketCapital, now)
  const authoritativeAShareHoldings = asharePositionAuthority.holdings
  const fallbackHoldings = mergeHoldings(
    [
      ...nonAuthoritativeHoldings.filter((holding) => holding.market !== 'A-share'),
      ...authoritativeAShareHoldings,
    ],
    planHoldings.filter((holding) => holding.market !== 'A-share'),
    simLedgerHoldings.filter((holding) => holding.market !== 'A-share'),
  )
  const equityPortfolio = await readEquitySnapshotPortfolio(projectRoot, generatedAt)
  const ashareAccount = await readAShareAccountSummary(projectRoot, ashareMarketCapital)
  const ashareTierSummaries = readAShareTierSummaries(ashareAccount)
  const ashareNoTradeExplanation = await readLatestAShareNoTradeExplanation(projectRoot, now)
  const ashareResearchEvidence = await readAShareResearchEvidence(toProjectPath(projectRoot, tradingAgentReadModelSources.ashareResearchEvidence))
  const ashareCanonicalProjectionSet = await readCurrentAShareProjectionSet(
    join(projectRoot, 'shared/review/ashare'),
  )
  const rawAShareSampleKpi = ashareCanonicalProjectionSet
    ? readAShareSampleKpi(ashareCanonicalProjectionSet.sampleKpi)
    : undefined
  const ashareSampleKpi = rawAShareSampleKpi && ashareMarketCapital
    && sameAShareCapitalAuthority(rawAShareSampleKpi.authorityScope, ashareMarketCapital)
    ? rawAShareSampleKpi
    : undefined
  const rawAShareMarketMaturity = ashareCanonicalProjectionSet
    ? readAShareMarketMaturity(ashareCanonicalProjectionSet.marketMaturity)
    : undefined
  const ashareMarketMaturity = rawAShareMarketMaturity && ashareSampleKpi
    && sameAShareProjectionAuthority(rawAShareMarketMaturity.authorityScope, ashareSampleKpi.authorityScope)
    && ashareMarketCapital
    && sameAShareCapitalAuthority(rawAShareMarketMaturity.authorityScope, ashareMarketCapital)
    ? rawAShareMarketMaturity
    : undefined
  const rawCNFuturesMarketMaturity = await readCNFuturesMarketMaturity(
    toProjectPath(projectRoot, tradingAgentReadModelSources.cnFuturesMarketMaturity),
  )
  const cnFuturesMarketMaturity = rawCNFuturesMarketMaturity && cnFuturesMarketCapital
    && sameCNFuturesCapitalAuthority(
      rawCNFuturesMarketMaturity.authorityScope,
      cnFuturesMarketCapital,
    )
    ? rawCNFuturesMarketMaturity
    : undefined
  const ashareForwardValidation = sampleKpiCompatibilityView(ashareSampleKpi)
  const paperDayRun = await readPaperDayRunSummary(
    toProjectPath(projectRoot, tradingAgentReadModelSources.paperDayRunBundle),
  )
  const cnFuturesReplayEvidence = await readCNFuturesReplayEvidence(toProjectPath(projectRoot, tradingAgentReadModelSources.cnFuturesReplay))
  const equityPerformance = ashareAccount && isAShareLegacyEquitySummary(equityPortfolio.summary) ? [] : equityPortfolio.performance
  const performance = annotatePerformanceQuality(firstNonEmpty(equityPerformance, reviewPerformance))
  const portfolio = attachAShareAccountSummary(equityPortfolio.summary, ashareAccount)
  const marketSummaries = await buildMarketSummaries({
    holdings: fallbackHoldings,
    ashareNoTradeExplanation,
    reviewRoot,
    projectRoot,
    portfolio,
    signals,
    simLedgerRoot,
    cnFuturesReplayEvidence,
    ashareMarketCapital,
    asharePositionAuthorityState: asharePositionAuthority.state,
    cnFuturesMarketCapital,
    ashareMarketMaturity,
    cnFuturesMarketMaturity,
    now,
  })
  const marketPulseRead = await readSharedSignalsMarketPulses({
    baseUrl: process.env.TRADINGDATAS_API_URL,
    expectedCatalogVersion: process.env.TRADINGDATAS_CATALOG_VERSION,
    accessPolicyId: process.env.TRADINGDATAS_ACCESS_POLICY_ID,
    schemaMajor: parseTradingDatasSchemaMajor(process.env.TRADINGDATAS_SCHEMA_MAJOR),
    datasetIds: parseMarketPulseDatasetIds(process.env.TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON),
    holdings: fallbackHoldings,
    signals,
    now,
  })
  const hasOrders = await directoryHasJson(filledSignalsPath)
  const hasPlan = await fileExists(positionPlanPath)
  const hasReview = await fileExists(reviewPath) || await fileExists(reviewFallbackPath)
  const hasSimLedger = simLedgerHoldings.length > 0 || simLedgerSignals.length > 0
  const hasPerformanceEvidence = hasReview || equityPortfolio.summary !== undefined
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
    marketPulses: marketPulseRead.pulses,
    marketPulseCoverage: marketPulseRead.coverage,
    marketPulseCoverageHistory: marketPulseRead.coverageHistory,
    ashareResearchEvidence,
    ashareSampleKpi,
    ashareMarketMaturity,
    cnFuturesMarketMaturity,
    ashareForwardValidation,
    ashareTierSummaries,
    paperDayRun,
    sourceRefs: tradingAgentReadModelSources,
  }
}

const PAPER_DAY_STAGE_ORDER = [
  'preopen',
  'evidence_ready',
  'universe_ready',
  'decision_ready',
  'risk_checked',
  'orders_simulated',
  'reconciled',
  'learning_recorded',
  'reported',
] as const

const PAPER_DAY_STATUSES = new Set([
  'incomplete',
  'incomplete_with_blocks',
  'completed',
  'completed_with_blocks',
])

type PaperDayReceipt = {
  stage: string
  status: string
  idempotencyKey: string
  component: PaperDayComponent
  inputBundleSha256: string
  payload: Record<string, unknown>
  payloadSha256: string
  reasonCodes: string[]
  receiptId: string
}

type PaperDayComponent = {
  stage: string | null
  componentId: string
  version: string
  artifactSha256: string
}

const PAPER_DAY_EXECUTION_POSITION_INVALIDATING_REASONS = new Set([
  'execution_authority_proof_invalid',
  'execution_receipt_state_invalid',
  'execution_receipt_time_invalid',
  'execution_risk_order_mismatch',
  'execution_time_precedes_decision',
  'execution_without_risk_order',
  'fill_quantity_conservation_invalid',
  'non_mainboard_execution_leak',
  'order_receipt_missing',
  'unfilled_receipt_proof_invalid',
  'unknown_simulated_order',
])

async function readPaperDayRunSummary(path: string): Promise<PaperDayRunSummary | undefined> {
  let encoded: Buffer
  try {
    encoded = await readRegularFile(path)
  } catch {
    return undefined
  }
  let payload: Record<string, unknown>
  try {
    payload = asRecord(JSON.parse(encoded.toString('utf8')) as unknown)
  } catch {
    return undefined
  }
  const context = asRecord(payload.context)
  const projection = asRecord(payload._projection)
  if (
    !hasExactKeys(payload, [
      '_projection',
      'block_reasons',
      'component_manifest_sha256',
      'components',
      'context',
      'contract_id',
      'exit_evaluation_allowed',
      'permitted_order_ids',
      'position_authority_valid',
      'run_id',
      'stage_receipts',
      'status',
      'stop_new_risk',
    ])
    || payload.contract_id !== 'tradingagent.paper_day_loop.v1'
    || !isContractText(payload.run_id)
    || !hasExactKeys(context, [
      'account_type',
      'authority_generation',
      'authority_id',
      'champion_manifest_sha256',
      'decision_as_of',
      'execution_lineage',
      'market',
      'real_trading_enabled',
      'trade_date',
    ])
    || context.market !== 'ashare'
    || !isContractText(context.authority_id)
    || !Number.isInteger(context.authority_generation)
    || Number(context.authority_generation) <= 0
    || !isContractText(context.execution_lineage)
    || context.account_type !== 'simulated'
    || context.real_trading_enabled !== false
    || !isIsoDate(context.trade_date)
    || !canonicalShanghaiDecisionAsOf(context.decision_as_of, context.trade_date)
    || !isSha256(context.champion_manifest_sha256)
    || !hasExactKeys(projection, [
      'authority',
      'bundle_sha256',
      'environment',
      'production_verified',
      'record_type',
      'schema_version',
    ])
    || projection.authority !== 'non_authority'
    || !isSha256(projection.bundle_sha256)
    || projection.environment !== 'local_candidate'
    || projection.production_verified !== false
    || projection.record_type !== 'run_bundle_projection'
    || projection.schema_version !== 1
    || !isSha256(payload.component_manifest_sha256)
    || typeof payload.stop_new_risk !== 'boolean'
    || typeof payload.position_authority_valid !== 'boolean'
    || payload.exit_evaluation_allowed !== payload.position_authority_valid
    || !PAPER_DAY_STATUSES.has(String(payload.status))
  ) return undefined

  const components = readPaperDayComponents(payload.components)
  if (!components) return undefined
  if (sha256Text(canonicalJson(components.map(componentRecord))) !== payload.component_manifest_sha256) return undefined
  const expectedRunId = canonicalPaperDayRunId(context)
  if (!expectedRunId || payload.run_id !== expectedRunId) return undefined
  const canonicalRoot = canonicalRootFromPublishedProjection(encoded.toString('utf8'), projection)
  if (!canonicalRoot || sha256Text(canonicalRoot) !== projection.bundle_sha256) return undefined
  try {
    const immutable = await readRegularFile(join(
      dirname(path),
      'runs',
      String(payload.run_id),
      `${String(projection.bundle_sha256)}.json`,
    ))
    if (!immutable.equals(encoded)) return undefined
  } catch {
    return undefined
  }
  const blockReasons = stringArrayStrict(payload.block_reasons)
  const permittedOrderIds = stringArrayStrict(payload.permitted_order_ids)
  if (!blockReasons || !permittedOrderIds) return undefined
  const receipts = readPaperDayReceipts(payload.stage_receipts, components, canonicalRoot, payload, context)
  if (!receipts) return undefined
  const accumulatedReasons = uniqueStrings(receipts.flatMap((receipt) => receipt.reasonCodes))
  if (!sameStringArray(blockReasons, accumulatedReasons)) return undefined
  const derivedStopNewRisk = receipts.some((receipt) => paperDayReceiptStopsNewRisk(receipt))
  if (payload.stop_new_risk !== derivedStopNewRisk) return undefined
  const expectedStatus = receipts.length === PAPER_DAY_STAGE_ORDER.length
    ? payload.stop_new_risk ? 'completed_with_blocks' : 'completed'
    : payload.stop_new_risk ? 'incomplete_with_blocks' : 'incomplete'
  if (payload.status !== expectedStatus) return undefined
  const receipt = (stage: string) => receipts.find((row) => row.stage === stage)
  const evidencePayload = receipt('evidence_ready')?.payload
  const universePayload = receipt('universe_ready')?.payload
  const decisionPayload = receipt('decision_ready')?.payload
  const riskPayload = receipt('risk_checked')?.payload
  const ordersPayload = receipt('orders_simulated')?.payload
  if (
    decisionPayload
    && decisionPayload.champion_manifest_sha256 !== undefined
    && decisionPayload.champion_manifest_sha256 !== context.champion_manifest_sha256
  ) return undefined

  const datasets = recordArray(evidencePayload?.datasets)
  const dataEvidenceState: PaperDayRunSummary['dataEvidenceState'] = datasets.length === 0
    ? 'unavailable'
    : datasets.every((row) => row.state === 'ready' && row.evidence_action === 'accept')
      ? 'ready'
      : 'degraded'
  const requiredDatasets = datasets.filter((row) => row.role === 'required_execution')
  const executionEvidenceEligible = evidencePayload?.execution_eligible === true
    && requiredDatasets.length > 0
    && requiredDatasets.every((row) => (
      row.state === 'ready'
      && row.evidence_action === 'accept'
      && row.effective_weight === 1
      && isContractText(row.receipt_id)
    ))
  const candidates = stringArray(universePayload?.feasible_symbols)
  const decisions = recordArray(decisionPayload?.decisions)
  const approvedOrders = recordArray(riskPayload?.approved_orders)
  const orderReceipts = recordArray(ordersPayload?.order_receipts)
  const riskBlocks = uniqueStrings([
    ...stringArray(payload.block_reasons),
    ...(payload.position_authority_valid ? [] : ['position_authority_invalid']),
  ])
  const noTradeReasons = uniqueStrings([
    ...stringArray(riskPayload?.no_trade_reasons),
    ...stringArray(decisionPayload?.no_trade_reasons),
    ...stringArray(receipt('reported')?.payload.no_trade_reasons),
  ])
  const blocked = payload.stop_new_risk || !executionEvidenceEligible || riskBlocks.length > 0
  const simulationExecutionState: PaperDayRunSummary['simulationExecutionState'] = blocked
    ? 'blocked'
    : approvedOrders.length > 0
      ? 'eligible'
      : 'no_orders'
  const llmEvidence = asRecord(decisionPayload?.llm_evidence)

  return {
    environment: 'local_candidate',
    productionVerified: false,
    contractId: 'tradingagent.paper_day_loop.v1',
    runId: payload.run_id,
    tradeDate: context.trade_date as string,
    status: payload.status as PaperDayRunSummary['status'],
    currentStage: receipts.at(-1)?.stage,
    completedStageCount: receipts.length,
    totalStageCount: 9,
    dataEvidenceState,
    simulationExecutionState,
    candidateCount: candidates.length,
    decisionCount: decisions.length,
    simulatedOrderCount: orderReceipts.length,
    simulatedFillCount: orderReceipts.filter((row) => ['filled', 'partial'].includes(String(row.status))).length,
    noTradeReasons,
    riskBlocks,
    championManifestSha256: context.champion_manifest_sha256 as string,
    llmEvidenceState: llmEvidence.role === 'evidence_only' ? 'evidence_only' : 'unavailable',
    source: 'shared/runtime/run_bundles/latest.json',
  }
}

function readPaperDayComponents(value: unknown): PaperDayComponent[] | undefined {
  if (!Array.isArray(value)) return undefined
  const components: PaperDayComponent[] = []
  const stageComponents: string[] = []
  const identities = new Set<string>()
  for (const candidate of value) {
    const row = asRecord(candidate)
    if (!hasExactKeys(row, ['artifact_sha256', 'component_id', 'stage', 'version'])) return undefined
    const stage = row.stage === null ? null : isPaperDayStage(row.stage) ? row.stage : undefined
    const componentId = isContractText(row.component_id) ? row.component_id : undefined
    const version = isContractText(row.version) ? row.version : undefined
    const artifactSha256 = isSha256(row.artifact_sha256) ? row.artifact_sha256 : undefined
    if (stage === undefined || !componentId || !version || !artifactSha256) return undefined
    const identity = `${stage ?? 'null'}:${componentId}`
    if (identities.has(identity)) return undefined
    identities.add(identity)
    if (stage) stageComponents.push(stage)
    components.push({ stage, componentId, version, artifactSha256 })
  }
  if (!sameStringArray(stageComponents, [...PAPER_DAY_STAGE_ORDER])) return undefined
  return components
}

function readPaperDayReceipts(
  value: unknown,
  components: PaperDayComponent[],
  canonicalRoot: string,
  root: Record<string, unknown>,
  context: Record<string, unknown>,
): PaperDayReceipt[] | undefined {
  if (!Array.isArray(value) || value.length > PAPER_DAY_STAGE_ORDER.length) return undefined
  const finalBlockReasons = stringArrayStrict(root.block_reasons)
  const finalPermittedOrderIds = stringArrayStrict(root.permitted_order_ids)
  if (!finalBlockReasons || !finalPermittedOrderIds) return undefined
  const rawReceiptArray = rawTopLevelProperty(canonicalRoot, 'stage_receipts')
  const rawReceipts = rawReceiptArray ? splitRawJsonArray(rawReceiptArray) : undefined
  if (!rawReceipts || rawReceipts.length !== value.length) return undefined
  const receipts: PaperDayReceipt[] = []
  const sealedReceipts: Record<string, unknown>[] = []
  let stopNewRisk = false
  let positionAuthorityValid = false
  let blockReasons: string[] = []
  let permittedOrderIds: string[] = []
  for (const [index, candidate] of value.entries()) {
    const row = asRecord(candidate)
    const reasonCodes = stringArrayStrict(row.reason_codes)
    const expectedComponent = components.find((component) => component.stage === PAPER_DAY_STAGE_ORDER[index])
    const rawPayload = rawTopLevelProperty(rawReceipts[index], 'payload')
    if (
      !hasExactKeys(row, [
        'component',
        'idempotency_key',
        'input_bundle_sha256',
        'payload',
        'payload_sha256',
        'reason_codes',
        'receipt_id',
        'stage',
        'status',
      ])
      || row.stage !== PAPER_DAY_STAGE_ORDER[index]
      || !['completed', 'completed_with_blocks'].includes(String(row.status))
      || !isSha256(row.idempotency_key)
      || !isSha256(row.input_bundle_sha256)
      || !isSha256(row.payload_sha256)
      || !isSha256(row.receipt_id)
      || !reasonCodes
      || !expectedComponent
      || canonicalJson(asRecord(row.component)) !== canonicalJson(componentRecord(expectedComponent))
      || !rawPayload
      || sha256Text(rawPayload) !== row.payload_sha256
      || (reasonCodes.length === 0) !== (row.status === 'completed')
    ) return undefined
    const expectedInputBundleSha256 = sha256Text(canonicalJson(paperDayBundleRecord({
      root,
      context,
      components,
      stageReceipts: sealedReceipts,
      stopNewRisk,
      positionAuthorityValid,
      blockReasons,
      permittedOrderIds,
    })))
    if (row.input_bundle_sha256 !== expectedInputBundleSha256) return undefined
    const expectedIdempotencyKey = sha256Text(canonicalJson({
      run_id: root.run_id,
      stage: row.stage,
      input_bundle_sha256: expectedInputBundleSha256,
      component_id: expectedComponent.componentId,
      component_version: expectedComponent.version,
      component_artifact_sha256: expectedComponent.artifactSha256,
    }))
    if (row.idempotency_key !== expectedIdempotencyKey) return undefined
    const receiptIdentity = {
      stage: row.stage,
      status: row.status,
      idempotency_key: row.idempotency_key,
      component: componentRecord(expectedComponent),
      input_bundle_sha256: row.input_bundle_sha256,
      payload_sha256: row.payload_sha256,
      reason_codes: reasonCodes,
    }
    if (sha256Text(canonicalJson(receiptIdentity)) !== row.receipt_id) return undefined
    const receipt = {
      stage: String(row.stage),
      status: String(row.status),
      idempotencyKey: row.idempotency_key,
      component: expectedComponent,
      inputBundleSha256: row.input_bundle_sha256,
      payload: asRecord(row.payload),
      payloadSha256: row.payload_sha256,
      reasonCodes,
      receiptId: row.receipt_id,
    } satisfies PaperDayReceipt
    const priorStopNewRisk = stopNewRisk
    receipts.push(receipt)
    sealedReceipts.push(row)
    blockReasons = uniqueStrings([...blockReasons, ...reasonCodes])
    stopNewRisk = stopNewRisk || paperDayReceiptStopsNewRisk(receipt)
    positionAuthorityValid = paperDayPositionAuthorityAfter(
      positionAuthorityValid,
      receipt,
    )
    if (receipt.stage === 'risk_checked') {
      const validated = validatedPaperDayPermittedOrderIds({
        payload: receipt.payload,
        reasonCodes,
        priorStopNewRisk,
        positionAuthorityValid,
        persistedOrderIds: finalPermittedOrderIds,
      })
      if (!validated) return undefined
      permittedOrderIds = validated
    }
  }
  const rebuiltRoot = paperDayBundleRecord({
    root,
    context,
    components,
    stageReceipts: sealedReceipts,
    stopNewRisk,
    positionAuthorityValid,
    blockReasons,
    permittedOrderIds,
  })
  if (canonicalJson(rebuiltRoot) !== canonicalRoot) return undefined
  return receipts
}

function paperDayBundleRecord({
  root,
  context,
  components,
  stageReceipts,
  stopNewRisk,
  positionAuthorityValid,
  blockReasons,
  permittedOrderIds,
}: {
  root: Record<string, unknown>
  context: Record<string, unknown>
  components: PaperDayComponent[]
  stageReceipts: Record<string, unknown>[]
  stopNewRisk: boolean
  positionAuthorityValid: boolean
  blockReasons: string[]
  permittedOrderIds: string[]
}) {
  const complete = stageReceipts.length === PAPER_DAY_STAGE_ORDER.length
  return {
    contract_id: root.contract_id,
    run_id: root.run_id,
    context,
    components: components.map(componentRecord),
    component_manifest_sha256: root.component_manifest_sha256,
    stage_receipts: stageReceipts,
    stop_new_risk: stopNewRisk,
    position_authority_valid: positionAuthorityValid,
    exit_evaluation_allowed: positionAuthorityValid,
    block_reasons: blockReasons,
    permitted_order_ids: permittedOrderIds,
    status: complete
      ? stopNewRisk ? 'completed_with_blocks' : 'completed'
      : stopNewRisk ? 'incomplete_with_blocks' : 'incomplete',
  }
}

function paperDayReceiptStopsNewRisk(receipt: PaperDayReceipt) {
  if (receipt.reasonCodes.length > 0) return true
  return receipt.stage === 'risk_checked'
    && asRecord(receipt.payload.drift_constraint).stop_new_orders === true
}

function paperDayPositionAuthorityAfter(
  current: boolean,
  receipt: PaperDayReceipt,
) {
  if (receipt.stage === 'preopen') {
    return receipt.payload.position_authority_valid === true
  }
  if (receipt.stage === 'orders_simulated') {
    const hasPositionChangingFill = recordArray(receipt.payload.order_receipts)
      .some((row) => ['filled', 'partial'].includes(String(row.status).toLowerCase()))
    const invalidated = receipt.reasonCodes.some((reason) => (
      PAPER_DAY_EXECUTION_POSITION_INVALIDATING_REASONS.has(reason)
    ))
    return hasPositionChangingFill || invalidated ? false : current
  }
  if (receipt.stage === 'reconciled') {
    return receipt.reasonCodes.length === 0
      && receipt.payload.position_authority_valid === true
  }
  return current
}

function validatedPaperDayPermittedOrderIds({
  payload,
  reasonCodes,
  priorStopNewRisk,
  positionAuthorityValid,
  persistedOrderIds,
}: {
  payload: Record<string, unknown>
  reasonCodes: string[]
  priorStopNewRisk: boolean
  positionAuthorityValid: boolean
  persistedOrderIds: string[]
}): string[] | undefined {
  const driftStopsRisk = asRecord(payload.drift_constraint).stop_new_orders === true
  const seenOrderIds = new Set<string>()
  const candidateOrders: Array<{ orderId: string, intent: string }> = []
  let riskOrderContractInvalid = !Array.isArray(payload.approved_orders)
  const rawOrders = Array.isArray(payload.approved_orders) ? payload.approved_orders : []
  for (const candidate of rawOrders) {
    const order = asRecord(candidate)
    const orderId = isContractText(order.order_id) ? order.order_id : undefined
    const intent = typeof order.intent === 'string' ? order.intent.toLowerCase() : undefined
    if (
      !orderId
      || seenOrderIds.has(orderId)
      || !intent
      || !['open', 'increase', 'reduce', 'exit'].includes(intent)
    ) {
      riskOrderContractInvalid = true
      continue
    }
    seenOrderIds.add(orderId)
    candidateOrders.push({ orderId, intent })
  }
  const blockNewRisk = priorStopNewRisk
    || reasonCodes.length > 0
    || driftStopsRisk
    || riskOrderContractInvalid
  const derivedOrderIds: string[] = []
  for (const { orderId, intent } of candidateOrders) {
    if (['open', 'increase'].includes(intent)) {
      if (blockNewRisk) continue
    } else if (['reduce', 'exit'].includes(intent)) {
      if (!positionAuthorityValid) continue
    } else {
      continue
    }
    derivedOrderIds.push(orderId)
  }
  return sameStringArray(derivedOrderIds, persistedOrderIds)
    ? derivedOrderIds
    : undefined
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(asRecord).filter((row) => Object.keys(row).length > 0) : []
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((row): row is string => typeof row === 'string' && row.length > 0)
}

function uniqueStrings(values: string[]) {
  return [...new Set(values)]
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const parsed = new Date(`${value}T00:00:00.000Z`)
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}

function isAwareIsoInstant(value: unknown): value is string {
  if (
    typeof value !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  ) return false
  return !Number.isNaN(new Date(value).getTime())
}

function canonicalShanghaiDecisionAsOf(
  value: unknown,
  tradeDate: unknown,
): string | undefined {
  if (
    typeof value !== 'string'
    || typeof tradeDate !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/.test(value)
  ) return undefined
  const instant = Date.parse(value)
  if (!Number.isFinite(instant)) return undefined
  const shanghaiWallClock = new Date(instant + 8 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 19)
  const canonical = `${shanghaiWallClock}+08:00`
  if (canonical !== value || value.slice(0, 10) !== tradeDate) return undefined
  return canonical
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  return actual.length === wanted.length
    && actual.every((key, index) => key === wanted[index])
}

function isPaperDayStage(value: unknown): value is typeof PAPER_DAY_STAGE_ORDER[number] {
  return typeof value === 'string' && (PAPER_DAY_STAGE_ORDER as readonly string[]).includes(value)
}

function componentRecord(component: PaperDayComponent) {
  return {
    stage: component.stage,
    component_id: component.componentId,
    version: component.version,
    artifact_sha256: component.artifactSha256,
  }
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const rows = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
    return `{${rows.join(',')}}`
  }
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('non-finite JSON number')
  const encoded = JSON.stringify(value)
  if (encoded === undefined) throw new Error('non-JSON value')
  return encoded
}

function sha256Text(value: string) {
  return createHash('sha256').update(value).digest('hex')
}

function canonicalPaperDayRunId(context: Record<string, unknown>) {
  try {
    const identity = {
      contract_id: 'tradingagent.paper_day_loop.v1',
      trade_date: context.trade_date,
      decision_as_of: context.decision_as_of,
      market: context.market,
      authority_id: context.authority_id,
      authority_generation: context.authority_generation,
      execution_lineage: context.execution_lineage,
      account_type: context.account_type,
      real_trading_enabled: context.real_trading_enabled,
    }
    return `ashare-paper-day-${sha256Text(canonicalJson(identity)).slice(0, 32)}`
  } catch {
    return undefined
  }
}

function canonicalRootFromPublishedProjection(
  encoded: string,
  projection: Record<string, unknown>,
) {
  if (!encoded.endsWith('\n') || encoded.slice(0, -1).includes('\n')) return undefined
  const body = encoded.slice(0, -1)
  let projectionCanonical: string
  try {
    projectionCanonical = canonicalJson(projection)
  } catch {
    return undefined
  }
  const prefix = `{"_projection":${projectionCanonical},`
  if (!body.startsWith(prefix) || !body.endsWith('}')) return undefined
  return `{${body.slice(prefix.length)}`
}

function stringArrayStrict(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const rows: string[] = []
  for (const candidate of value) {
    if (!isContractText(candidate) || rows.includes(candidate)) return undefined
    rows.push(candidate)
  }
  return rows
}

function sameStringArray(left: readonly string[], right: readonly string[]) {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function rawTopLevelProperty(objectText: string, expectedKey: string): string | undefined {
  let cursor = skipJsonWhitespace(objectText, 0)
  if (objectText[cursor] !== '{') return undefined
  cursor += 1
  while (cursor < objectText.length) {
    cursor = skipJsonWhitespace(objectText, cursor)
    if (objectText[cursor] === '}') return undefined
    if (objectText[cursor] !== '"') return undefined
    const keyEnd = scanJsonStringEnd(objectText, cursor)
    if (keyEnd === undefined) return undefined
    let key: string
    try {
      key = JSON.parse(objectText.slice(cursor, keyEnd)) as string
    } catch {
      return undefined
    }
    cursor = skipJsonWhitespace(objectText, keyEnd)
    if (objectText[cursor] !== ':') return undefined
    cursor = skipJsonWhitespace(objectText, cursor + 1)
    const valueEnd = scanJsonValueEnd(objectText, cursor)
    if (valueEnd === undefined) return undefined
    if (key === expectedKey) return objectText.slice(cursor, valueEnd)
    cursor = skipJsonWhitespace(objectText, valueEnd)
    if (objectText[cursor] === ',') {
      cursor += 1
      continue
    }
    if (objectText[cursor] === '}') return undefined
    return undefined
  }
  return undefined
}

function splitRawJsonArray(arrayText: string): string[] | undefined {
  let cursor = skipJsonWhitespace(arrayText, 0)
  if (arrayText[cursor] !== '[') return undefined
  cursor += 1
  const rows: string[] = []
  while (cursor < arrayText.length) {
    cursor = skipJsonWhitespace(arrayText, cursor)
    if (arrayText[cursor] === ']') return rows
    const valueEnd = scanJsonValueEnd(arrayText, cursor)
    if (valueEnd === undefined) return undefined
    rows.push(arrayText.slice(cursor, valueEnd))
    cursor = skipJsonWhitespace(arrayText, valueEnd)
    if (arrayText[cursor] === ',') {
      cursor += 1
      continue
    }
    if (arrayText[cursor] === ']') return rows
    return undefined
  }
  return undefined
}

function skipJsonWhitespace(value: string, start: number) {
  let cursor = start
  while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1
  return cursor
}

function scanJsonStringEnd(value: string, start: number): number | undefined {
  if (value[start] !== '"') return undefined
  let escaped = false
  for (let cursor = start + 1; cursor < value.length; cursor += 1) {
    const character = value[cursor]
    if (escaped) {
      escaped = false
      continue
    }
    if (character === '\\') {
      escaped = true
      continue
    }
    if (character === '"') return cursor + 1
  }
  return undefined
}

function scanJsonValueEnd(value: string, start: number): number | undefined {
  const first = value[start]
  if (first === '"') return scanJsonStringEnd(value, start)
  if (first === '{' || first === '[') {
    const stack = [first]
    let inString = false
    let escaped = false
    for (let cursor = start + 1; cursor < value.length; cursor += 1) {
      const character = value[cursor]
      if (inString) {
        if (escaped) escaped = false
        else if (character === '\\') escaped = true
        else if (character === '"') inString = false
        continue
      }
      if (character === '"') {
        inString = true
        continue
      }
      if (character === '{' || character === '[') stack.push(character)
      else if (character === '}' || character === ']') {
        const opening = stack.pop()
        if ((opening === '{' && character !== '}') || (opening === '[' && character !== ']')) return undefined
        if (stack.length === 0) return cursor + 1
      }
    }
    return undefined
  }
  let cursor = start
  while (cursor < value.length && ![',', '}', ']'].includes(value[cursor])) cursor += 1
  const raw = value.slice(start, cursor).trim()
  if (!raw) return undefined
  try {
    JSON.parse(raw)
  } catch {
    return undefined
  }
  return start + value.slice(start, cursor).lastIndexOf(raw) + raw.length
}

function isContractText(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && value === value.trim()
    && /^[A-Za-z0-9._:-]+$/.test(value)
}

function isSafeExecutionLineageId(value: unknown): value is string {
  return isContractText(value)
    && value !== '.'
    && value !== '..'
    && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)
}

function executionLineageDir(projectRoot: string, executionLineageId: string) {
  if (!isSafeExecutionLineageId(executionLineageId)) return undefined
  return join(projectRoot, 'shared/logs/execution_lineages', executionLineageId)
}

function parseMarketPulseDatasetIds(
  raw: string | undefined,
): Partial<Record<Exclude<Market, 'All Markets'>, string>> | undefined {
  if (!raw?.trim()) return undefined
  try {
    const payload = asRecord(JSON.parse(raw))
    const allowedMarkets = new Set<Exclude<Market, 'All Markets'>>([
      'A-share',
      'Crypto',
      'CNFutures',
    ])
    const datasetIds: Partial<Record<Exclude<Market, 'All Markets'>, string>> = {}
    for (const [market, value] of Object.entries(payload)) {
      if (!allowedMarkets.has(market as Exclude<Market, 'All Markets'>)) return undefined
      const datasetId = optionalString(value)
      if (!datasetId || !/^[a-z0-9][a-z0-9._-]*$/.test(datasetId)) return undefined
      datasetIds[market as Exclude<Market, 'All Markets'>] = datasetId
    }
    return Object.keys(datasetIds).length ? datasetIds : undefined
  } catch {
    return undefined
  }
}

function parseTradingDatasSchemaMajor(raw: string | undefined): number | undefined {
  if (!raw || raw !== raw.trim() || !/^[1-9]\d*$/.test(raw)) return undefined
  const value = Number(raw)
  return Number.isSafeInteger(value) ? value : undefined
}

async function readAShareAccountSummary(
  projectRoot: string,
  marketCapital: MarketCapitalProjection | undefined,
): Promise<PortfolioSummary['ashareAccount'] | undefined> {
  if (!marketCapital || marketCapital.market !== 'A-share') return undefined
  const localSimDir = executionLineageDir(projectRoot, marketCapital.executionLineageId)
  if (!localSimDir) return undefined
  const pnlPayload = await readOptionalJson(join(localSimDir, 'local_sim_pnl.json'))
  const pnlRows = asRecord(pnlPayload)
  const accountPnl = selectASharePnlAccount(pnlRows)
  const localScopeMatches = accountPnl?.capital_authority_id === marketCapital.authorityId
    && parseFiniteNumber(accountPnl.authority_generation) === marketCapital.authorityGeneration
    && accountPnl.execution_lineage_id === marketCapital.executionLineageId
  const cashAvailable = marketCapital.cashBalanceCny
  const marketValue = marketCapital.positionsMarketValueCny
  const accountEquity = marketCapital.equityCny
  const capitalBase = marketCapital.initialEquityCny
  const accountTotalPnl = roundMoney(accountEquity - capitalBase)
  const accountRealizedPnl = marketCapital.realizedPnlCny
  const accountUnrealizedPnl = marketCapital.unrealizedPnlCny
  const sampleQuality = await readAShareSampleQuality(
    join(localSimDir, 'local_sim_trades.jsonl'),
    marketCapital,
  )
  const openPositionCount = marketCapital.openPositionCount

  return {
    capitalAuthorityId: 'ashare-capital-v1',
    authorityGeneration: marketCapital.authorityGeneration,
    executionLineageId: marketCapital.executionLineageId,
    cashAvailable: roundMoney(cashAvailable),
    marketValue: roundMoney(marketValue),
    accountEquity,
    accountTotalPnl: roundMoney(accountTotalPnl),
    accountRealizedPnl: roundMoney(accountRealizedPnl),
    accountUnrealizedPnl: roundMoney(accountUnrealizedPnl),
    accountReturnPct: roundMetric(capitalBase > 0 ? (accountTotalPnl / capitalBase) * 100 : 0),
    openPositionCount,
    totalSampleCount: sampleQuality.totalSampleCount,
    validationSampleCount: sampleQuality.validationSampleCount,
    strategySampleValidCount: sampleQuality.strategySampleValidCount,
    strategyTotalPnl: localScopeMatches && sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? roundMoney(accountTotalPnl)
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    strategyMarketValue: localScopeMatches && sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? roundMoney(marketValue)
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    strategyOpenPositionCount: localScopeMatches && sampleQuality.strategySampleValidCount === sampleQuality.totalSampleCount
      ? openPositionCount
      : sampleQuality.strategySampleValidCount === 0
        ? 0
        : undefined,
    source: marketCapital.source,
    updatedAt: marketCapital.updatedAt,
  }
}

async function readLatestAShareNoTradeExplanation(projectRoot: string, now: Date): Promise<AShareNoTradeExplanation | undefined> {
  const path = join(projectRoot, 'shared/logs/ashare_no_trade_explanations.jsonl')
  const dayKey = formatShanghaiDateKey(now)
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    for (const line of lines.reverse()) {
      try {
        const row = JSON.parse(line) as AShareNoTradeLogRow
        const rowDay = compactDate(row.date ?? row.generated_at)
        if (rowDay !== dayKey) continue
        const explanation = asRecord(row.no_trade_explanation) as AShareNoTradeExplanation
        if (explanation.category) return explanation
      } catch {
        // Ignore malformed no-trade rows; newer valid rows remain usable.
      }
    }
  } catch {
    // Fresh workspaces may not have no-trade attribution yet.
  }
  return undefined
}

function attachAShareAccountSummary(
  summary: PortfolioSummary | undefined,
  ashareAccount: PortfolioSummary['ashareAccount'] | undefined,
): PortfolioSummary | undefined {
  if (!ashareAccount) return summary
  if (summary && !isAShareLegacyEquitySummary(summary)) return { ...summary, ashareAccount }
  return {
    pnlAmount: ashareAccount.accountTotalPnl,
    returnPct: ashareAccount.accountReturnPct,
    capitalBase: roundMoney(ashareAccount.accountEquity - ashareAccount.accountTotalPnl),
    targetPct: DEFAULT_TARGET_RETURN_PCT,
    maxDrawdownPct: Math.max(0, -ashareAccount.accountReturnPct),
    tradeCount: ashareAccount.totalSampleCount,
    pointCount: 1,
    source: ashareAccount.source,
    pnlSource: 'ashare_local_sim_account',
    pnlCurrency: 'CNY',
    realizedPnl: ashareAccount.accountRealizedPnl ?? 0,
    unrealizedPnl: ashareAccount.accountUnrealizedPnl ?? ashareAccount.accountTotalPnl,
    ashareAccount,
    updatedAt: ashareAccount.updatedAt,
  }
}

function readAShareTierSummaries(
  mainAccount?: PortfolioSummary['ashareAccount'],
): AShareTierSummary[] | undefined {
  const summaries: AShareTierSummary[] = []
  if (mainAccount) {
    summaries.push({
      account: 'ashare_sim',
      label: `${Math.round((mainAccount.accountEquity - mainAccount.accountTotalPnl) / 10_000)}万主账户`,
      capital: roundMoney(mainAccount.accountEquity - mainAccount.accountTotalPnl),
      totalPnl: mainAccount.accountTotalPnl,
      returnPct: mainAccount.accountReturnPct,
      marketValue: mainAccount.marketValue,
      cashAvailable: mainAccount.cashAvailable,
      tradeCount: mainAccount.totalSampleCount,
      source: mainAccount.source,
      updatedAt: mainAccount.updatedAt,
    })
  }

  return summaries.length > 0 ? summaries : undefined
}

function isAShareLegacyEquitySummary(summary: PortfolioSummary | undefined) {
  return summary?.pnlSource === 'ashare_local_sim_mark_to_market'
}

function mergeHoldings(...sources: HoldingRow[][]): HoldingRow[] {
  const rows = new Map<string, HoldingRow>()
  for (const source of sources) {
    for (const holding of source) {
      const key = `${holding.market}:${holding.accountScope ?? 'unscoped'}:${holding.symbol}:${holding.role}`
      if (!rows.has(key)) rows.set(key, holding)
    }
  }
  return [...rows.values()]
}

function mergeSignals(...sources: SignalRow[][]): SignalRow[] {
  const rows = new Map<string, SignalRow>()
  for (const source of sources) {
    for (const signal of source) {
      const key = signal.opportunityId
        ? `opportunity:${signal.opportunityId}`
        : `${signal.market}:${signal.symbol}:${signal.method}:${signal.status}:${signal.stage ?? ''}:${signal.age}`
      if (!rows.has(key)) rows.set(key, signal)
    }
  }
  return [...rows.values()].sort((a, b) => Number.parseInt(a.age, 10) - Number.parseInt(b.age, 10))
}

async function buildMarketSummaries({
  holdings,
  ashareNoTradeExplanation,
  reviewRoot,
  projectRoot,
  portfolio,
  signals,
  simLedgerRoot,
  cnFuturesReplayEvidence,
  ashareMarketCapital,
  asharePositionAuthorityState,
  cnFuturesMarketCapital,
  ashareMarketMaturity,
  cnFuturesMarketMaturity,
  now,
}: {
  holdings: HoldingRow[]
  ashareNoTradeExplanation?: AShareNoTradeExplanation
  reviewRoot: string
  projectRoot: string
  portfolio?: PortfolioSummary
  signals: SignalRow[]
  simLedgerRoot: string
  cnFuturesReplayEvidence?: CNFuturesReplayEvidence
  ashareMarketCapital?: MarketCapitalProjection
  asharePositionAuthorityState: ASharePositionAuthorityState
  cnFuturesMarketCapital?: MarketCapitalProjection
  ashareMarketMaturity?: AShareMarketMaturityProjection
  cnFuturesMarketMaturity?: CNFuturesMarketMaturityProjection
  now: Date
}): Promise<MarketSummary[]> {
  const cnFuturesReviewSummary = await readCNFuturesReviewMarketSummary(reviewRoot)
  const equitySummaries = await readEquitySnapshotMarketSummaries(simLedgerRoot)
  const healthSummaries = await readSimMarketHealthSummaries(projectRoot, now)

  return DASHBOARD_MARKETS.map((market) => {
    const holdingCount = holdings.filter((holding) => holding.market === market).length
    const marketSignals = signals.filter((signal) => signal.market === market)
    const executedCount = marketSignals.filter((signal) => signal.status === 'executed').length
    const styleSummary = market === 'CNFutures' ? cnFuturesReviewSummary : undefined
    const marketCapital = market === 'A-share'
      ? ashareMarketCapital
      : market === 'CNFutures'
        ? cnFuturesMarketCapital
        : undefined
    const performanceSummary = marketCapital
      ? marketCapitalPerformanceSummary(marketCapital)
      : market === 'A-share' || market === 'CNFutures'
        ? undefined
        : equitySummaries.get(market)
    const isAshare = market === 'A-share'
    const ashareAccount = isAshare ? portfolio?.ashareAccount : undefined
    const rawCapitalBase = marketCapital?.initialEquityCny ?? (ashareAccount
      ? roundMoney(ashareAccount.accountEquity - ashareAccount.accountTotalPnl)
      : performanceSummary?.capitalBase)
    const capitalBase = validatedMarketCapitalBase(market, rawCapitalBase, Boolean(marketCapital))
    const pnlAmount = marketCapital
      ? roundMoney(marketCapital.equityCny - marketCapital.initialEquityCny)
      : ashareAccount?.accountTotalPnl ?? performanceSummary?.pnl
    const returnPct = ashareAccount
      ? ashareAccount.accountReturnPct
      : pnlAmount !== undefined && capitalBase && capitalBase > 0
        ? roundMetric((pnlAmount / capitalBase) * 100)
        : undefined
    const tradeCount = ashareAccount
      ? ashareAccount.totalSampleCount
      : executedCount > 0 ? executedCount : styleSummary?.filledCount ?? performanceSummary?.trades ?? 0
    const styleCount = Math.max(styleSummary?.styleCount ?? 0, styleSummary?.activeStyleCount ?? 0)
    const hasMeaningfulPnl = pnlAmount !== undefined && (pnlAmount !== 0 || (capitalBase ?? 0) > 0 || (performanceSummary?.trades ?? 0) > 0)
      const hasRuntime = holdingCount > 0 || marketSignals.length > 0 || tradeCount > 0 || styleCount > 0 || hasMeaningfulPnl
      const hasPartialEvidence = Boolean(performanceSummary || styleSummary)
      const hasOnlyStyleSummary = styleCount > 0 && holdingCount === 0 && marketSignals.length === 0 && pnlAmount === undefined
      const positionAuthorityUnavailable = isAshare && asharePositionAuthorityState === 'unavailable'
      const evidenceStatus: MarketSummary['status'] = hasRuntime ? hasOnlyStyleSummary ? 'partial' : 'ready' : hasPartialEvidence ? 'partial' : 'empty'
      const status: MarketSummary['status'] = positionAuthorityUnavailable ? 'paused' : evidenceStatus
      const evidenceRuntimeState = marketRuntimeState({
        errorCount: styleSummary?.errorCount,
        filledCount: styleSummary?.filledCount,
        holdingCount,
        signalCount: marketSignals.length,
        status,
        styleCount,
        tradeCount,
      })
      const healthSummary = healthSummaries.get(market)
      const runtimeState = positionAuthorityUnavailable
        ? 'needs_attention'
        : healthSummary ? runtimeStateFromHealth(healthSummary, evidenceRuntimeState) : evidenceRuntimeState
      const latestAt = latestIso(styleSummary?.latestAt, performanceSummary?.latestAt, marketCapital?.updatedAt, ashareAccount?.updatedAt)
      const baseHeadline = buildMarketSummaryHeadline(market, status, holdingCount, marketSignals.length, tradeCount, styleCount)
      const baseDetail = buildMarketSummaryDetail({
        activeStyleCount: styleSummary?.activeStyleCount,
        ashareNoTradeExplanation: isAshare && tradeCount <= 0 ? ashareNoTradeExplanation : undefined,
        capitalBase,
        errorCount: styleSummary?.errorCount,
        filledCount: styleSummary?.filledCount,
        pnlAmount: hasMeaningfulPnl ? pnlAmount : undefined,
        returnPct,
        styleCount,
      })

      return {
        market,
        status,
        runtimeState,
        executionFault: positionAuthorityUnavailable || (healthSummary?.executionFault ?? runtimeState === 'needs_attention'),
        runtimeReason: positionAuthorityUnavailable
          ? 'ashare_position_authority_unavailable'
          : healthSummary?.reasons[0],
        noTradeEvidence: isAshare
          ? buildAShareNoTradeEvidence(ashareNoTradeExplanation, marketCapital)
          : undefined,
        cnFuturesReplayEvidence: market === 'CNFutures' ? cnFuturesReplayEvidence : undefined,
        cnFuturesMaturityEvidence: market === 'CNFutures' ? cnFuturesMarketMaturity : undefined,
        capitalUtilizationPct: marketCapital?.capitalUtilizationPct,
        deployedCapitalCny: marketCapital?.deployedCapitalCny,
        availableToReserveCny: marketCapital?.availableToReserveCny,
        riskUsedCny: marketCapital?.riskUsedCny,
        riskLimitCny: marketCapital?.riskLimitCny,
        undeployedReasons: buildMarketUndeployedReasons({
          ashareNoTradeExplanation: isAshare ? ashareNoTradeExplanation : undefined,
          cnFuturesMarketMaturity: market === 'CNFutures' ? cnFuturesMarketMaturity : undefined,
        }),
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
      pnlCurrency: performanceSummary?.currency ?? marketNativeCurrency(market),
      returnPct,
      maxDrawdownPct: ashareAccount
        ? roundMetric(Math.max(0, -ashareAccount.accountReturnPct))
        : performanceSummary ? roundMetric(Math.abs(performanceSummary.maxDrawdown)) : undefined,
      realizedPnl: ashareAccount
        ? ashareAccount.accountRealizedPnl ?? 0
        : marketCapital
          ? roundMoney(marketCapital.realizedPnlCny)
          : performanceSummary ? roundMoney(performanceSummary.realizedPnl) : undefined,
      unrealizedPnl: ashareAccount
        ? ashareAccount.accountUnrealizedPnl ?? ashareAccount.accountTotalPnl
        : marketCapital
          ? roundMoney(marketCapital.unrealizedPnlCny)
          : performanceSummary ? roundMoney(performanceSummary.unrealizedPnl) : undefined,
      latestAt,
      source: marketCapital
        ? marketCapital.source
        : isAshare && ashareAccount
          ? ashareAccount.source
        : styleSummary?.source ?? (performanceSummary
          ? tradingAgentReadModelSources.equitySnapshots
          : tradingAgentReadModelSources.simLedger),
      capitalAuthorityId: marketCapital?.authorityId ?? null,
      authorityGeneration: marketCapital?.authorityGeneration ?? null,
      executionLineageId: marketCapital?.executionLineageId ?? null,
      maturity: market === 'A-share'
        ? ashareMarketMaturity?.stage ?? null
        : market === 'CNFutures'
          ? cnFuturesMarketMaturity?.stage ?? null
          : null,
      headline: positionAuthorityUnavailable
        ? 'A-share 持仓权威不可用'
        : healthSummary ? buildHealthAwareHeadline(market, healthSummary, baseHeadline) : baseHeadline,
      detail: positionAuthorityUnavailable
        ? '当前资本快照存在持仓，但匹配的 execution lineage 持仓回执缺失、损坏或冲突；未回退旧账本。'
        : healthSummary ? buildHealthAwareDetail(healthSummary, baseDetail) : baseDetail,
      }
  })
}

function buildMarketUndeployedReasons({
  ashareNoTradeExplanation,
  cnFuturesMarketMaturity,
}: {
  ashareNoTradeExplanation?: AShareNoTradeExplanation
  cnFuturesMarketMaturity?: CNFuturesMarketMaturityProjection
}): MarketSummary['undeployedReasons'] {
  const rows: NonNullable<MarketSummary['undeployedReasons']> = []
  const capitalPlan = asRecord(ashareNoTradeExplanation?.capital_plan_decision)
  const capitalPlanAudit = asRecord(capitalPlan.audit)
  const rawReasons = Array.isArray(capitalPlan.undeployed_reasons)
    ? capitalPlan.undeployed_reasons
    : Array.isArray(capitalPlanAudit.undeployed_reasons)
      ? capitalPlanAudit.undeployed_reasons
      : []
  for (const rawReason of rawReasons) {
    const reason = asRecord(rawReason)
    const code = optionalString(reason.code)
    if (!code) continue
    const amountCny = parseFiniteNumber(reason.amount_cny as number | string | undefined)
    const details = optionalString(reason.details)
    rows.push({
      code,
      ...(amountCny === undefined ? {} : { amountCny: roundMoney(amountCny) }),
      ...(details ? { details } : {}),
    })
  }
  if (!rows.length && ashareNoTradeExplanation?.category) {
    rows.push({ code: ashareNoTradeExplanation.category })
  }
  for (const code of cnFuturesMarketMaturity?.blockingReasons ?? []) {
    rows.push({ code })
  }
  const unique = new Map<string, (typeof rows)[number]>()
  for (const row of rows) {
    if (!unique.has(row.code)) unique.set(row.code, row)
  }
  return unique.size ? [...unique.values()] : undefined
}

async function readSimMarketHealthSummaries(projectRoot: string, now: Date): Promise<Map<Market, SimMarketHealthSummary>> {
  const payload = asRecord(await readOptionalJson(toProjectPath(projectRoot, tradingAgentReadModelSources.simMarketHealth)))
  if (isStaleSimMarketHealth(payload.generated_at, now)) return new Map()
  const checks = Array.isArray(payload.checks) ? payload.checks as SimMarketHealthCheck[] : []
  const summaries = new Map<Market, SimMarketHealthSummary>()
  for (const check of checks) {
    if (!check || typeof check !== 'object') continue
    const market = normalizeHealthMarket(check.details?.market ?? check.name)
    if (!market) continue
    const reasons = [
      ...(Array.isArray(check.details?.fail_reasons) ? check.details?.fail_reasons ?? [] : []),
      ...(Array.isArray(check.details?.warn_reasons) ? check.details?.warn_reasons ?? [] : []),
    ].map((item) => String(item)).filter(Boolean).sort(compareRuntimeReasons)
    summaries.set(market, {
      status: check.status ?? 'warn',
      summary: optionalString(check.summary),
      diagnosticClass: optionalString(check.details?.diagnostic_class),
      executionFault: Boolean(check.details?.execution_fault),
      reasons,
    })
  }
  return summaries
}

function compareRuntimeReasons(left: string, right: string): number {
  return runtimeReasonRank(left) - runtimeReasonRank(right)
}

function runtimeReasonRank(reason: string): number {
  if (reason.includes('_waiting_')) return 0
  if (reason.startsWith('latest_cron_status=')) return 1
  if (reason === 'market_data_degraded') return 2
  return 1
}

function isStaleSimMarketHealth(generatedAt: unknown, now: Date): boolean {
  if (!generatedAt) return false
  const time = Date.parse(String(generatedAt))
  if (!Number.isFinite(time)) return true
  return now.getTime() - time > MAX_SIM_MARKET_HEALTH_AGE_MS
}

function normalizeHealthMarket(value: unknown): Market | undefined {
  const text = String(value ?? '').toLowerCase()
  if (text.includes('ashare') || text.includes('a-share')) return 'A-share'
  if (text.includes('crypto')) return 'Crypto'
  if (text.includes('cn_futures') || text.includes('cnfutures') || text.includes('futures')) return 'CNFutures'
  return undefined
}

function runtimeStateFromHealth(health: SimMarketHealthSummary, fallback: MarketSummary['runtimeState']): MarketSummary['runtimeState'] {
  if (health.status === 'fail' || health.executionFault || health.diagnosticClass === 'execution_fault') return 'needs_attention'
  if (health.diagnosticClass === 'strategy_wait') return 'strategy_wait'
  if (health.diagnosticClass === 'market_data_wait') return 'empty'
  if (health.reasons.some((reason) => reason.includes('_waiting_') || reason === 'server_local_sim_has_no_production_trades_yet')) return 'strategy_wait'
  if (health.status === 'pass') return 'normal'
  return fallback
}

function buildHealthAwareHeadline(market: Market, health: SimMarketHealthSummary, fallback: string): string {
  if (health.summary) return health.summary.replace(/^([a-z_]+)\s+/i, `${market} `)
  return fallback
}

function buildHealthAwareDetail(health: SimMarketHealthSummary, fallback: string): string {
  if (!health.reasons.length) return fallback
  return `${fallback} · 当前原因 ${health.reasons[0]}`
}

function marketRuntimeState({
  errorCount,
  filledCount,
  holdingCount,
  signalCount,
  status,
  styleCount,
  tradeCount,
}: {
  errorCount?: number
  filledCount?: number
  holdingCount: number
  signalCount: number
  status: MarketSummary['status']
  styleCount: number
  tradeCount: number
}): MarketSummary['runtimeState'] {
  if ((errorCount ?? 0) > 0) return 'needs_attention'
  if (tradeCount > 0 || (filledCount ?? 0) > 0 || holdingCount > 0 || signalCount > 0) return 'normal'
  if (styleCount > 0 || status === 'partial') return 'strategy_wait'
  return 'empty'
}

async function readCNFuturesReviewMarketSummary(root: string): Promise<MarketStyleSummary | undefined> {
  const path = join(root, 'data/cn_futures_sim_reviews.jsonl')
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    for (const line of lines.reverse()) {
      try {
        const row = JSON.parse(line) as CNFuturesReviewRow
        const filledCount = Math.max(0, Math.trunc(parseFiniteNumber(row.filled_count) ?? 0))
        const holdCount = Math.max(0, Math.trunc(parseFiniteNumber(row.hold_count) ?? 0))
        const errorCount = Math.max(0, Math.trunc(parseFiniteNumber(row.error_count) ?? 0))
        const recordCount = Math.max(0, Math.trunc(parseFiniteNumber(row.record_count) ?? filledCount + holdCount + errorCount))
        if (recordCount <= 0 && filledCount <= 0 && holdCount <= 0 && errorCount <= 0) continue
        return {
          source: tradingAgentReadModelSources.cnFuturesReview,
          status: optionalString(row.state),
          styleCount: 1,
          activeStyleCount: filledCount > 0 ? 1 : 0,
          filledCount,
          errorCount,
          holdCount,
          recordCount,
          signalCount: Math.max(0, Math.trunc(parseFiniteNumber(row.signal_count) ?? 0)),
          latestAt: optionalString(row.generated_at),
        }
      } catch {
        // Ignore malformed append-only rows.
      }
    }
  } catch {
    return undefined
  }
  return undefined
}

async function readEquitySnapshotMarketSummaries(root: string): Promise<Map<Market, MarketPerformanceSummary>> {
  const latestBySource = new Map<string, { market: Market; snapshot: ParsedEquitySnapshot }>()

  for (const file of await listSimLedgerFiles(root, 'daily_mark_to_market.jsonl')) {
    const market = normalizeMarketFolder(file.market)
    if (market === 'All Markets') continue
    if (market === 'A-share' || market === 'CNFutures') continue
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      for (const line of lines) {
        try {
          const raw = JSON.parse(line) as EquitySnapshotRow
          if (isDashboardExcluded(raw as Record<string, unknown>)) continue
          const snapshot = parseEquitySnapshotRecord({ ...raw, sourcePath: file.path })
          if (!snapshot) continue
          const key = `${market}:${file.strategy}:${file.path}`
          const current = latestBySource.get(key)
          if (!current || snapshot.timestampMs >= current.snapshot.timestampMs) {
            latestBySource.set(key, { market, snapshot })
          }
        } catch {
          // Ignore malformed append-only rows.
        }
      }
    } catch {
      // Ignore missing optional snapshot files.
    }
  }

  const accountScopesByMarket = new Map<Market, Set<string>>()
  for (const { market, snapshot } of latestBySource.values()) {
    const scopes = accountScopesByMarket.get(market) ?? new Set<string>()
    scopes.add(dirname(snapshot.sourcePath))
    accountScopesByMarket.set(market, scopes)
  }

  const summaries = new Map<Market, MarketPerformanceSummary>()
  for (const { market, snapshot } of latestBySource.values()) {
    // A strategy directory is the narrowest available ledger/account scope.
    // Without a shared account authority, separate scopes must not be summed.
    if ((accountScopesByMarket.get(market)?.size ?? 0) !== 1) continue
    const current = summaries.get(market) ?? {
      capitalBase: 0,
      currency: snapshot.currency,
      maxDrawdown: 0,
      pnl: 0,
      realizedPnl: 0,
      trades: 0,
      unrealizedPnl: 0,
    }
    if (current.currency !== snapshot.currency) continue
    current.capitalBase = (current.capitalBase ?? 0) + snapshot.capitalBase
    current.pnl += snapshot.pnl
    current.realizedPnl += snapshot.realizedPnl
    current.unrealizedPnl += snapshot.unrealizedPnl
    current.maxDrawdown = Math.max(current.maxDrawdown, snapshot.maxDrawdownPct)
    current.trades += snapshot.tradeCount
    current.latestAt = latestIso(current.latestAt, snapshot.timestamp)
    summaries.set(market, current)
  }

  return summaries
}

function normalizeMarketFolder(value: string): Market {
  const normalized = value.trim().toLowerCase()
  if (normalized === 'ashare' || normalized === 'a-share' || normalized === 'a_share') return 'A-share'
  if (normalized === 'cn_futures' || normalized === 'cnfutures') return 'CNFutures'
  if (normalized === 'crypto') return 'Crypto'
  return 'All Markets'
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
  ashareNoTradeExplanation,
  capitalBase,
  errorCount,
  filledCount,
  pnlAmount,
  returnPct,
  styleCount,
}: {
  activeStyleCount?: number
  ashareNoTradeExplanation?: AShareNoTradeExplanation
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
  const noTrade = formatAShareNoTradeExplanation(ashareNoTradeExplanation)
  if (noTrade) facts.push(noTrade)
  if (styleCount > 0) facts.push(`风格 ${activeStyleCount ?? 0}/${styleCount}`)
  if (filledCount !== undefined) facts.push(`成交 ${filledCount}`)
  if (errorCount) facts.push(`失败 ${errorCount}`)
  return facts.length ? facts.join(' · ') : '等待该市场写入模拟成交、持仓或明确收益证据。'
}

function buildAShareNoTradeEvidence(
  explanation?: AShareNoTradeExplanation,
  marketCapital?: MarketCapitalProjection,
): AShareNoTradeEvidence | undefined {
  if (!explanation?.category) return undefined
  const counts = asRecord(explanation.counts)
  const candidateCount = firstParsedNumber(counts.candidates)
  const orderCount = firstParsedNumber(counts.orders)
  const candidateTrace = Array.isArray(explanation.candidate_decision_trace) ? explanation.candidate_decision_trace : []
  const capitalPlan = asRecord(explanation.capital_plan_decision)
  const sampleAdjustment = asRecord(capitalPlan.sample_adjustment)
  const portfolioDecision = asRecord(explanation.portfolio_decision)
  const evidenceGaps: string[] = []

  if ((candidateCount ?? 0) > 0 && (orderCount ?? 0) <= 0) {
    if (!candidateTrace.length) evidenceGaps.push('candidate_decision_trace_missing')
    if (!Object.keys(capitalPlan).length) evidenceGaps.push('capital_plan_decision_missing')
    if (!Object.keys(portfolioDecision).length) evidenceGaps.push('portfolio_decision_missing')
  }

  return {
    category: explanation.category,
    action: explanation.action,
    evidenceStatus: evidenceGaps.length ? 'incomplete' : 'ready',
    evidenceGaps,
    universeCount: firstParsedNumber(counts.universe),
    candidateCount,
    orderCount,
    riskRejectionCount: firstParsedNumber(counts.risk_rejections),
    skippedCandidateCount: firstParsedNumber(counts.skipped_candidates),
    executionSkipCount: firstParsedNumber(counts.execution_skips),
    candidateTraceCount: candidateTrace.length,
    capitalPlanCapacity: firstParsedNumber(capitalPlan.position_capacity),
    targetPositions: firstParsedNumber(capitalPlan.target_positions),
    riskMode: optionalString(capitalPlan.risk_mode),
    allowedBuyCount: firstParsedNumber(portfolioDecision.allowed_buy_count),
    accountCashAvailable: marketCapital?.cashBalanceCny
      ?? firstParsedNumber(capitalPlan.account_cash_available, sampleAdjustment.account_cash_available),
    strategyCashAvailable: marketCapital ? undefined : firstParsedNumber(capitalPlan.strategy_cash_available, capitalPlan.available_cash, sampleAdjustment.strategy_cash_available),
    accountPositionCount: marketCapital?.openPositionCount
      ?? firstParsedNumber(sampleAdjustment.account_position_count),
    strategyPositionCount: marketCapital ? undefined : firstParsedNumber(sampleAdjustment.strategy_position_count),
    ignoredValidationSampleCount: firstParsedNumber(sampleAdjustment.ignored_validation_sample_count),
    strategySampleValidCount: firstParsedNumber(sampleAdjustment.strategy_sample_valid_count),
  }
}

function formatAShareNoTradeExplanation(explanation?: AShareNoTradeExplanation) {
  if (!explanation?.category) return undefined
  const category = {
    all_candidates_missing_price: '候选缺价格',
    all_rejected_by_risk: '风控全部拦截',
    degraded_errors: '运行有降级错误',
    duplicate_existing_signal: '已有同日信号',
    execution_failed: '执行失败',
    execution_skipped: '执行前置跳过',
    no_candidates: '候选池暂无达标机会',
    no_filled_sim_orders: '暂无成交样本',
    no_portfolio_orders: '仓位计划未出单',
    no_universe: '数据入口未形成股票池',
    pending_execution: '等待执行结果',
    portfolio_empty: '资金或手数未形成订单',
  }[explanation.category] ?? explanation.category
  const action = formatAShareNoTradeAction(explanation.action)
  return action ? `无交易：${category}，${action}` : `无交易：${category}`
}

function formatAShareNoTradeAction(action?: string) {
  return {
    check_capital_lot_size_and_constructor_output: '检查资金和整手约束',
    check_candidate_pool_thresholds_and_universe_filter: '检查候选池阈值',
    check_position_sizing_and_portfolio_constructor: '检查仓位计划',
    check_sharedsignals_assets_and_daily_coverage: '检查数据覆盖',
    check_sharedsignals_daily_or_realtime_prices: '检查价格数据',
    review_execution_skip_reasons: '复盘执行跳过原因',
    review_failed_receipts: '复盘失败回执',
    review_full_sim_run: '复盘模拟主循环',
    review_orchestrator_errors: '检查运行错误',
    review_pending_signal_state: '检查待执行状态',
    review_risk_rejections: '复盘风控原因',
    review_same_day_idempotency_state: '检查同日幂等',
  }[String(action ?? '')]
}

function marketName(market: Market) {
  if (market === 'A-share') return 'A股'
  if (market === 'Crypto') return '加密'
  if (market === 'CNFutures') return '中国期货'
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

async function readAShareSampleQuality(
  path: string,
  marketCapital: MarketCapitalProjection,
) {
  const rows: LocalSimTradeRow[] = []
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    for (const line of lines) {
      try {
        const row = JSON.parse(line) as LocalSimTradeRow
        if (row.status && String(row.status).toLowerCase() !== 'filled') continue
        if (
          row.capital_authority_id !== marketCapital.authorityId
          || parseFiniteNumber(row.authority_generation) !== marketCapital.authorityGeneration
          || row.execution_lineage_id !== marketCapital.executionLineageId
        ) continue
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
  if (row.ashare_session_valid === false) return false
  if (!isAshareRegularSession(row)) return false
  if (side === 'buy') return source === 'ashare_candidate_layer' && layer === 'candidate'
  if (side === 'sell') return source === 'ashare_rebalance_sell'
  return false
}

function isAshareRegularSession(row: LocalSimTradeRow) {
  const raw = String(row.created_at ?? row.timestamp ?? row.filled_at ?? '').trim()
  if (!raw) return true
  const parsed = Date.parse(raw)
  if (!Number.isFinite(parsed)) return true
  const local = new Date(parsed + 8 * 60 * 60 * 1000)
  const weekday = local.getUTCDay()
  if (weekday === 0 || weekday === 6) return false
  const minutes = local.getUTCHours() * 60 + local.getUTCMinutes()
  return (minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30)
    || (minutes >= 13 * 60 && minutes <= 14 * 60 + 57)
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
      realTradingEnabled: false,
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
          predictionCount: parseFiniteNumber(styleSummary.prediction_count as number | string | undefined),
          explorationFillCount: parseFiniteNumber(styleSummary.exploration_fill_count as number | string | undefined),
          exploitationFillCount: parseFiniteNumber(styleSummary.exploitation_fill_count as number | string | undefined),
          completedRoundTripCount: parseFiniteNumber(styleSummary.completed_round_trip_count as number | string | undefined),
        },
      },
    }
  } catch {
    return undefined
  }
}

async function readMarketCapitalProjection(
  path: string,
  expectedMarket: 'A-share' | 'CNFutures',
  now: Date,
): Promise<MarketCapitalProjection | undefined> {
  let payload: Record<string, unknown>
  try {
    payload = asRecord(JSON.parse((await readRegularFile(path)).toString('utf8')))
  } catch {
    return undefined
  }
  const isAshare = expectedMarket === 'A-share'
  const expectedAuthorityId = isAshare ? 'ashare-capital-v1' : 'cn-futures-capital-v1'
  const expectedAccountName = isAshare ? 'ashare_sim' : 'cn_futures_sim'
  const expectedMarketName = isAshare ? 'ashare' : 'cn_futures'
  const initialEquityCny = parseFiniteNumber(payload.initial_equity_cny as number | string | undefined)
  const equityCny = parseFiniteNumber(payload.equity_cny as number | string | undefined)
  const cashBalanceCny = parseFiniteNumber(payload.cash_balance_cny as number | string | undefined)
  const positionsMarketValueCny = parseFiniteNumber(payload.positions_market_value_cny as number | string | undefined)
  const marginUsedCny = parseFiniteNumber(payload.margin_used_cny as number | string | undefined)
  const realizedPnlCny = parseFiniteNumber(payload.realized_pnl_cny as number | string | undefined)
  const unrealizedPnlCny = parseFiniteNumber(payload.unrealized_pnl_cny as number | string | undefined)
  const executionLineageId = optionalString(payload.execution_lineage_id)
  const authorityGeneration = parseFiniteNumber(payload.authority_generation as number | string | undefined)
  const exposureLimit = parseFiniteNumber(payload.stock_gross_exposure_limit_cny as number | string | undefined)
  const marginLimit = parseFiniteNumber(payload.margin_utilization_limit_cny as number | string | undefined)
  const frozenOrderCashCny = parseFiniteNumber(payload.frozen_order_cash_cny as number | string | undefined) ?? 0
  const frozenOrderMarginCny = parseFiniteNumber(payload.frozen_order_margin_cny as number | string | undefined) ?? 0
  const reservedCashCny = parseFiniteNumber(payload.reserved_cash_cny as number | string | undefined) ?? 0
  const reservedExposureCny = parseFiniteNumber(payload.reserved_exposure_cny as number | string | undefined) ?? 0
  const reservedMarginCny = parseFiniteNumber(payload.reserved_margin_cny as number | string | undefined) ?? 0
  const reportedAvailableToReserveCny = parseFiniteNumber(payload.available_to_reserve_cny as number | string | undefined)
  const reportedUtilizationRate = parseFiniteNumber(payload.capital_utilization_rate as number | string | undefined)
  const updatedAt = optionalString(payload.updated_at)
  if (
    payload.source !== 'market_capital_ledger'
    || payload.schema_version !== 'market-capital-snapshot.v2'
    || payload.authority_id !== expectedAuthorityId
    || authorityGeneration === undefined
    || !Number.isInteger(authorityGeneration)
    || authorityGeneration <= 0
    || payload.account_name !== expectedAccountName
    || payload.market !== expectedMarketName
    || payload.currency !== 'CNY'
    || initialEquityCny !== DEFAULT_SIM_CAPITAL_CNY
    || !isSafeExecutionLineageId(executionLineageId)
    || payload.real_trading_enabled !== false
    || !updatedAt
    || !isFreshEvidenceInstant(updatedAt, now, MAX_CAPITAL_AUTHORITY_AGE_MS)
    || equityCny === undefined
    || cashBalanceCny === undefined
    || positionsMarketValueCny === undefined
    || marginUsedCny === undefined
    || realizedPnlCny === undefined
    || unrealizedPnlCny === undefined
    || [
      frozenOrderCashCny,
      frozenOrderMarginCny,
      reservedCashCny,
      reservedExposureCny,
      reservedMarginCny,
    ].some((value) => value < 0)
    || (isAshare && exposureLimit !== 45_000)
    || (!isAshare && marginLimit !== 25_000)
  ) return undefined

  const derivedEquity = isAshare
    ? cashBalanceCny + positionsMarketValueCny
    : cashBalanceCny + unrealizedPnlCny
  if (Math.abs(derivedEquity - equityCny) > 0.011) return undefined
  const quantities = asRecord(payload.positions_quantity_by_risk_unit)
  const positionsQuantityByRiskUnit: Record<string, number> = {}
  for (const [riskUnit, rawQuantity] of Object.entries(quantities)) {
    const quantity = parseFiniteNumber(rawQuantity as number | string | undefined)
    if (!isContractText(riskUnit) || quantity === undefined || (isAshare && quantity < 0)) return undefined
    positionsQuantityByRiskUnit[riskUnit] = quantity
  }
  const openPositionCount = Object.values(positionsQuantityByRiskUnit).filter((value) => value !== 0).length
  const riskUsedCny = isAshare
    ? positionsMarketValueCny + frozenOrderCashCny + reservedExposureCny
    : marginUsedCny + frozenOrderMarginCny + reservedMarginCny
  const riskLimitCny = isAshare ? exposureLimit! : marginLimit!
  if (riskUsedCny > riskLimitCny + 0.011) return undefined
  const derivedCashCapacity = isAshare
    ? cashBalanceCny - frozenOrderCashCny - reservedCashCny
    : cashBalanceCny - frozenOrderCashCny - frozenOrderMarginCny - reservedCashCny - reservedMarginCny
  const derivedAvailableToReserveCny = Math.max(
    0,
    Math.min(derivedCashCapacity, riskLimitCny - riskUsedCny),
  )
  const derivedUtilizationRate = riskUsedCny / DEFAULT_SIM_CAPITAL_CNY
  if (
    reportedAvailableToReserveCny !== undefined
    && Math.abs(reportedAvailableToReserveCny - derivedAvailableToReserveCny) > 0.011
  ) return undefined
  if (
    reportedUtilizationRate !== undefined
    && Math.abs(reportedUtilizationRate - derivedUtilizationRate) > 0.000001
  ) return undefined
  return {
    market: expectedMarket,
    authorityId: expectedAuthorityId,
    authorityGeneration,
    executionLineageId,
    initialEquityCny: 50_000,
    equityCny,
    cashBalanceCny,
    positionsMarketValueCny,
    marginUsedCny,
    realizedPnlCny,
    unrealizedPnlCny,
    updatedAt,
    openPositionCount,
    positionsQuantityByRiskUnit,
    deployedCapitalCny: roundMoney(riskUsedCny),
    availableToReserveCny: roundMoney(derivedAvailableToReserveCny),
    capitalUtilizationPct: roundMetric(derivedUtilizationRate * 100),
    riskUsedCny: roundMoney(riskUsedCny),
    riskLimitCny: roundMoney(riskLimitCny),
    source: isAshare
      ? tradingAgentReadModelSources.ashareMarketCapital
      : tradingAgentReadModelSources.cnFuturesMarketCapital,
  }
}

function sameAShareCapitalAuthority(
  scope: AShareProjectionAuthority,
  capital: MarketCapitalProjection,
) {
  return capital.market === 'A-share'
    && scope.capitalAuthorityId === capital.authorityId
    && scope.authorityGeneration === capital.authorityGeneration
    && scope.executionLineageId === capital.executionLineageId
}

function sameCNFuturesCapitalAuthority(
  scope: CNFuturesProjectionAuthority,
  capital: MarketCapitalProjection,
) {
  return capital.market === 'CNFutures'
    && scope.capitalAuthorityId === capital.authorityId
    && scope.authorityGeneration === capital.authorityGeneration
    && scope.executionLineageId === capital.executionLineageId
}

function parseAShareProjectionAuthority(value: unknown): AShareProjectionAuthority | undefined {
  const authority = asRecord(value)
  const capitalAuthorityId = optionalString(authority.capital_authority_id)
  const authorityGeneration = parseFiniteNumber(authority.authority_generation as number | string | undefined)
  const executionLineageId = optionalString(authority.execution_lineage_id)
  if (
    capitalAuthorityId !== 'ashare-capital-v1'
    || authorityGeneration === undefined
    || !Number.isInteger(authorityGeneration)
    || authorityGeneration <= 0
    || !isSafeExecutionLineageId(executionLineageId)
  ) return undefined
  return {
    capitalAuthorityId,
    authorityGeneration,
    executionLineageId,
  }
}

function parseCNFuturesProjectionAuthority(value: unknown): CNFuturesProjectionAuthority | undefined {
  const authority = asRecord(value)
  const capitalAuthorityId = optionalString(authority.capital_authority_id)
  const authorityGeneration = parseFiniteNumber(authority.authority_generation as number | string | undefined)
  const executionLineageId = optionalString(authority.execution_lineage_id)
  if (
    capitalAuthorityId !== 'cn-futures-capital-v1'
    || authorityGeneration !== 1
    || !executionLineageId
  ) return undefined
  return {
    capitalAuthorityId,
    authorityGeneration: 1,
    executionLineageId,
  }
}

function sameAShareProjectionAuthority(
  left: AShareProjectionAuthority,
  right: AShareProjectionAuthority,
) {
  return left.capitalAuthorityId === right.capitalAuthorityId
    && left.authorityGeneration === right.authorityGeneration
    && left.executionLineageId === right.executionLineageId
}

function ashareProjectionIsSimOnly(payload: Record<string, unknown>) {
  return payload.real_trading_enabled === false
    && payload.live_execution_enabled === false
    && payload.automatic_promotion_enabled === false
    && payload.automatic_risk_expansion_enabled === false
}

type AShareCanonicalProjectionSet = {
  sampleKpi: Record<string, unknown>
  evolutionDecision: Record<string, unknown>
  marketMaturity: Record<string, unknown>
}

const ashareProjectionFilenames = [
  'sample_kpi_latest.json',
  'evolution_decision_latest.json',
  'market_maturity_latest.json',
] as const

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
}

export function canonicalAShareProjectionGenerationId(
  projectionInputSha256: string,
  projectionSha256: Record<string, unknown>,
) {
  if (!isSha256(projectionInputSha256)) throw new Error('projection_input_sha256_invalid')
  const filenames = [...ashareProjectionFilenames].sort()
  if (Object.keys(projectionSha256).sort().join(',') !== filenames.join(',')) {
    throw new Error('projection_sha_map_missing')
  }
  const canonicalProjectionShas: Record<string, string> = {}
  for (const filename of filenames) {
    const digest = projectionSha256[filename]
    if (!isSha256(digest)) throw new Error(`projection_sha_map_invalid:${filename}`)
    canonicalProjectionShas[filename] = digest
  }
  const canonicalIdentity = `${JSON.stringify({
    projection_input_sha256: projectionInputSha256,
    projection_sha256: canonicalProjectionShas,
  })}\n`
  return `ashare-sample-projection-${createHash('sha256').update(canonicalIdentity).digest('hex')}`
}

async function readRegularFile(path: string): Promise<Buffer> {
  const metadata = await lstat(path)
  if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error('projection_file_not_regular')
  return readFile(path)
}

async function readCurrentAShareProjectionSet(
  reviewDir: string,
): Promise<AShareCanonicalProjectionSet | undefined> {
  try {
    const currentRaw = await readRegularFile(join(reviewDir, 'projection_current.json'))
    const current = asRecord(JSON.parse(currentRaw.toString('utf8')) as unknown)
    const generationId = String(current.generation_id ?? '')
    const generationPath = `projection_generations/${generationId}`
    const manifestSha256 = current.generation_manifest_sha256
    const projectionInputSha256 = current.projection_input_sha256
    const expectedShas = asRecord(current.projection_sha256)
    if (
      current.schema_version !== 1
      || !/^ashare-sample-projection-[a-f0-9]{64}$/.test(generationId)
      || current.generation_path !== generationPath
      || current.generation_manifest !== 'generation_manifest.json'
      || !isSha256(manifestSha256)
      || !isSha256(projectionInputSha256)
      || current.real_trading_enabled !== false
      || Object.keys(expectedShas).sort().join(',') !== [...ashareProjectionFilenames].sort().join(',')
      || ashareProjectionFilenames.some((filename) => !isSha256(expectedShas[filename]))
    ) return undefined
    if (
      canonicalAShareProjectionGenerationId(projectionInputSha256, expectedShas)
      !== generationId
    ) return undefined

    const generationsDir = join(reviewDir, 'projection_generations')
    const generationsMetadata = await lstat(generationsDir)
    if (!generationsMetadata.isDirectory() || generationsMetadata.isSymbolicLink()) return undefined
    const generationDir = join(generationsDir, generationId)
    const generationMetadata = await lstat(generationDir)
    if (!generationMetadata.isDirectory() || generationMetadata.isSymbolicLink()) return undefined
    const manifestRaw = await readRegularFile(join(generationDir, 'generation_manifest.json'))
    if (createHash('sha256').update(manifestRaw).digest('hex') !== manifestSha256) return undefined
    const manifest = asRecord(JSON.parse(manifestRaw.toString('utf8')) as unknown)
    const manifestShas = asRecord(manifest.projection_sha256)
    if (
      manifest.schema_version !== 1
      || manifest.generation_id !== generationId
      || manifest.projection_input_sha256 !== projectionInputSha256
      || manifest.run_id !== current.run_id
      || manifest.generated_at !== current.generated_at
      || manifest.real_trading_enabled !== false
      || ashareProjectionFilenames.some(
        (filename) => manifestShas[filename] !== expectedShas[filename],
      )
    ) return undefined

    const projections: Record<string, Record<string, unknown>> = {}
    for (const filename of ashareProjectionFilenames) {
      const raw = await readRegularFile(join(generationDir, filename))
      if (createHash('sha256').update(raw).digest('hex') !== expectedShas[filename]) return undefined
      const payload = asRecord(JSON.parse(raw.toString('utf8')) as unknown)
      if (
        payload.projection_input_sha256 !== projectionInputSha256
        || !ashareProjectionIsSimOnly(payload)
      ) return undefined
      projections[filename] = payload
    }
    const sampleKpi = projections['sample_kpi_latest.json']
    const evolutionDecision = projections['evolution_decision_latest.json']
    const marketMaturity = projections['market_maturity_latest.json']
    if (
      sampleKpi.report_type !== 'sample_journal_kpi'
      || sampleKpi.evidence_source !== 'sample_journal_kpi'
      || evolutionDecision.report_type !== 'ashare_evolution_decision_v2'
      || evolutionDecision.evidence_source !== 'sample_journal_kpi'
      || evolutionDecision.live_transition_authorized !== false
      || marketMaturity.report_type !== 'ashare_market_maturity_v1'
      || marketMaturity.evidence_source !== 'sample_journal_kpi'
      || marketMaturity.live_transition_authorized !== false
    ) return undefined
    const authorities = [sampleKpi, evolutionDecision, marketMaturity]
      .map((payload) => parseAShareProjectionAuthority(payload.authority_scope))
    if (
      authorities.some((authority) => !authority)
      || !sameAShareProjectionAuthority(authorities[0]!, authorities[1]!)
      || !sameAShareProjectionAuthority(authorities[0]!, authorities[2]!)
    ) return undefined
    return { sampleKpi, evolutionDecision, marketMaturity }
  } catch {
    return undefined
  }
}

function readAShareSampleKpi(value: unknown): AShareSampleKpiProjection | undefined {
  const payload = asRecord(value)
  const authorityScope = parseAShareProjectionAuthority(payload.authority_scope)
  if (
    payload.report_type !== 'sample_journal_kpi'
    || payload.evidence_source !== 'sample_journal_kpi'
    || !authorityScope
    || !ashareProjectionIsSimOnly(payload)
  ) return undefined

  const layers = asRecord(payload.sample_layer_totals)
  const styles = asRecord(payload.styles)
  const styleProjections: AShareSampleKpiProjection['styles'] = []
  let candidateCount = 0
  let predictionCount = 0
  let riskRejectCount = 0
  let readyForwardLabelCount = 0
  let pendingForwardLabelCount = 0
  for (const [styleId, rawStyle] of Object.entries(styles)) {
    const style = asRecord(rawStyle)
    const styleCandidateCount = nonnegativeInteger(style.candidate_count)
    const stylePredictionCount = nonnegativeInteger(style.prediction_count)
    const styleRiskRejectCount = nonnegativeInteger(style.risk_reject_count)
    let styleReadyForwardLabelCount = 0
    let stylePendingForwardLabelCount = 0
    candidateCount += styleCandidateCount
    predictionCount += stylePredictionCount
    riskRejectCount += styleRiskRejectCount
    const horizons = asRecord(style.forward_label_counts)
    for (const rawStatuses of Object.values(horizons)) {
      const statuses = asRecord(rawStatuses)
      for (const [status, rawCount] of Object.entries(statuses)) {
        const count = nonnegativeInteger(rawCount)
        if (status === 'ready' || status === 'labeled') {
          readyForwardLabelCount += count
          styleReadyForwardLabelCount += count
        } else {
          pendingForwardLabelCount += count
          stylePendingForwardLabelCount += count
        }
      }
    }
    const rejectionDistribution = asRecord(style.rejection_reason_distribution)
    styleProjections.push({
      styleId,
      candidateCount: styleCandidateCount,
      predictionCount: stylePredictionCount,
      observationCounterfactualCount: nonnegativeInteger(style.observation_counterfactual_count),
      explorationFillCount: nonnegativeInteger(style.exploration_fill_count),
      exploitationFillCount: nonnegativeInteger(style.exploitation_fill_count),
      completedRoundTripCount: nonnegativeInteger(style.completed_round_trip_count),
      readyForwardLabelCount: styleReadyForwardLabelCount,
      pendingForwardLabelCount: stylePendingForwardLabelCount,
      riskRejectCount: styleRiskRejectCount,
      winRate: parseNullableNumber(style.win_rate),
      expectancyCny: parseNullableNumber(style.expectancy_cny),
      postCostPnlCny: parseNullableNumber(style.post_cost_pnl_cny),
      maxDrawdownCny: parseNullableNumber(style.max_drawdown_cny),
      rejectionReasons: Object.entries(rejectionDistribution)
        .map(([reason, count]) => ({ reason, count: nonnegativeInteger(count) }))
        .filter((item) => item.count > 0)
        .sort((left, right) => right.count - left.count || left.reason.localeCompare(right.reason)),
    })
  }
  styleProjections.sort((left, right) => left.styleId.localeCompare(right.styleId))
  const scientificEvidence = asRecord(payload.scientific_evidence)
  return {
    source: 'sample_journal_kpi',
    generatedAt: String(payload.generated_at ?? ''),
    tradeDate: String(payload.trade_date ?? ''),
    authorityScope,
    journalEventCount: nonnegativeInteger(payload.journal_event_count),
    candidateCount,
    predictionCount,
    observationCounterfactualCount: nonnegativeInteger(layers.observation_counterfactual),
    explorationFillCount: nonnegativeInteger(layers.exploration_fill),
    exploitationFillCount: nonnegativeInteger(layers.exploitation_fill),
    completedRoundTripCount: nonnegativeInteger(layers.completed_round_trip),
    riskRejectCount: nonnegativeInteger(layers.risk_reject) || riskRejectCount,
    readyForwardLabelCount,
    pendingForwardLabelCount,
    styles: styleProjections,
    promotionEvidenceReady: scientificEvidence.promotion_evidence_ready === true,
    automaticPromotionEnabled: false,
    automaticRiskExpansionEnabled: false,
    realTradingEnabled: false,
  }
}

function readAShareMarketMaturity(value: unknown): AShareMarketMaturityProjection | undefined {
  const payload = asRecord(value)
  const authorityScope = parseAShareProjectionAuthority(payload.authority_scope)
  if (
    payload.report_type !== 'ashare_market_maturity_v1'
    || payload.evidence_source !== 'sample_journal_kpi'
    || !authorityScope
    || !ashareProjectionIsSimOnly(payload)
    || payload.live_transition_authorized !== false
  ) return undefined
  const checkpointDue = parseFiniteNumber(payload.checkpoint_due as number | string | undefined)
  return {
    source: 'sample_journal_kpi',
    generatedAt: String(payload.generated_at ?? ''),
    tradeDate: String(payload.trade_date ?? ''),
    authorityScope,
    stage: String(payload.stage ?? 'missing'),
    totalTradingDays: nonnegativeInteger(payload.total_trading_days),
    checkpointDue: checkpointDue === undefined ? undefined : Math.max(0, Math.trunc(checkpointDue)),
    promotionEvidenceReady: payload.promotion_evidence_ready === true,
    liveTransitionAuthorized: false,
    automaticPromotionEnabled: false,
    automaticRiskExpansionEnabled: false,
    realTradingEnabled: false,
  }
}

async function readCNFuturesMarketMaturity(
  path: string,
): Promise<CNFuturesMarketMaturityProjection | undefined> {
  const payload = asRecord(await readOptionalJson(path))
  const authorityScope = parseCNFuturesProjectionAuthority(payload.authority_scope)
  if (
    !hasValidCNFuturesMaturityProjectionHash(payload)
    || payload.report_type !== 'cn_futures_market_maturity_v1'
    || payload.evidence_source !== 'cn_futures_review_journal+sample_kpi'
    || !authorityScope
    || parseFiniteNumber(payload.pool_cny as number | string | undefined) !== 50_000
    || parseFiniteNumber(payload.margin_utilization_limit_cny as number | string | undefined) !== 25_000
    || payload.automatic_promotion_enabled !== false
    || payload.automatic_risk_expansion_enabled !== false
    || payload.live_transition_authorized !== false
    || payload.real_trading_enabled !== false
  ) return undefined

  const sampleCounts = asRecord(payload.sample_counts)
  const coverage = asRecord(payload.coverage)
  const performance = asRecord(payload.performance)
  const simulationTradingDays = Array.isArray(payload.simulation_trading_days)
    ? payload.simulation_trading_days.map(String).filter((value) => /^\d{8}$/.test(value))
    : []
  const totalSimulationTradingDays = nonnegativeInteger(payload.total_simulation_trading_days)
  if (simulationTradingDays.length !== totalSimulationTradingDays) return undefined
  const products = Array.isArray(coverage.products)
    ? coverage.products.map(String).filter(Boolean)
    : []
  const volatilityRegimes = Array.isArray(coverage.volatility_regimes)
    ? coverage.volatility_regimes.map(String).filter(Boolean)
    : []
  const productCount = nonnegativeInteger(coverage.product_count)
  const volatilityRegimeCount = nonnegativeInteger(coverage.volatility_regime_count)
  if (productCount !== products.length || volatilityRegimeCount !== volatilityRegimes.length) {
    return undefined
  }
  const blockingReasons = Array.isArray(payload.blocking_reasons)
    ? payload.blocking_reasons.map(String).filter(Boolean)
    : []
  return {
    source: 'cn_futures_review_journal+sample_kpi',
    generatedAt: String(payload.generated_at ?? ''),
    tradeDate: String(payload.trade_date ?? ''),
    freshStartTradeDate: String(payload.fresh_start_trade_date ?? ''),
    authorityScope,
    capitalPoolCny: 50_000,
    marginUtilizationLimitCny: 25_000,
    stage: String(payload.stage ?? 'missing'),
    simulationTradingDays,
    totalSimulationTradingDays,
    sampleCounts: {
      validSampleCount: nonnegativeInteger(sampleCounts.valid_sample_count),
      observationCounterfactualCount: nonnegativeInteger(sampleCounts.observation_counterfactual_count),
      counterfactualOnlyCount: nonnegativeInteger(sampleCounts.counterfactual_only_count),
      executionEligibleSampleCount: nonnegativeInteger(sampleCounts.execution_eligible_sample_count),
      completedRoundTripCount: nonnegativeInteger(sampleCounts.completed_round_trip_count),
      forwardLabelCount: nonnegativeInteger(sampleCounts.forward_label_count),
      pendingForwardLabelCount: nonnegativeInteger(sampleCounts.pending_forward_label_count),
      riskRejectCount: nonnegativeInteger(sampleCounts.risk_reject_count),
    },
    coverage: {
      products,
      productCount,
      volatilityRegimes,
      volatilityRegimeCount,
      nightSessionSampleCount: nonnegativeInteger(coverage.night_session_sample_count),
      rolloverSampleCount: nonnegativeInteger(coverage.rollover_sample_count),
      marginEvidenceSampleCount: nonnegativeInteger(coverage.margin_evidence_sample_count),
      feeEvidenceSampleCount: nonnegativeInteger(coverage.fee_evidence_sample_count),
      slippageEvidenceSampleCount: nonnegativeInteger(coverage.slippage_evidence_sample_count),
      extremeRiskSampleCount: nonnegativeInteger(coverage.extreme_risk_sample_count),
    },
    performance: {
      winRate: parseNullableNumber(performance.win_rate),
      expectancyCny: parseNullableNumber(performance.expectancy_cny),
      postCostPnlCny: parseNullableNumber(performance.post_cost_pnl_cny),
      maxDrawdownCny: parseNullableNumber(performance.max_drawdown_cny),
      stabilityScore: parseNullableNumber(performance.stability_score),
    },
    blockingReasons,
    promotionEvidenceReady: payload.promotion_evidence_ready === true,
    automaticPromotionEnabled: false,
    automaticRiskExpansionEnabled: false,
    liveTransitionAuthorized: false,
    realTradingEnabled: false,
  }
}

function normalizeCNFuturesMaturityProjectionValue(value: unknown): unknown {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('projection_number_must_be_finite')
    return Object.is(value, -0) ? 0 : value
  }
  if (Array.isArray(value)) {
    return value.map(normalizeCNFuturesMaturityProjectionValue)
  }
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, normalizeCNFuturesMaturityProjectionValue(record[key])]),
    )
  }
  throw new TypeError(`projection_value_not_json_safe:${typeof value}`)
}

export function canonicalCNFuturesMaturityProjectionSha256(
  projection: Record<string, unknown>,
): string {
  const payload = Object.fromEntries(
    Object.entries(projection).filter(([key]) => key !== 'projection_sha256'),
  )
  const canonical = JSON.stringify(normalizeCNFuturesMaturityProjectionValue(payload))
  return createHash('sha256').update(canonical, 'utf8').digest('hex')
}

function hasValidCNFuturesMaturityProjectionHash(
  projection: Record<string, unknown>,
): boolean {
  const provided = String(projection.projection_sha256 ?? '').trim().toLowerCase()
  if (!/^[a-f0-9]{64}$/.test(provided)) return false
  try {
    return canonicalCNFuturesMaturityProjectionSha256(projection) === provided
  } catch {
    return false
  }
}

function sampleKpiCompatibilityView(
  sampleKpi: AShareSampleKpiProjection | undefined,
): AShareForwardValidation | undefined {
  if (!sampleKpi) return undefined
  return {
    generatedAt: sampleKpi.generatedAt,
    date: sampleKpi.tradeDate,
    readOnly: true,
    realTradingEnabled: false,
    tradeCount: sampleKpi.completedRoundTripCount,
    strategyLabelCount: sampleKpi.readyForwardLabelCount,
    pendingCount: sampleKpi.pendingForwardLabelCount,
  }
}

function nonnegativeInteger(value: unknown) {
  return Math.max(
    0,
    Math.trunc(parseFiniteNumber(value as number | string | undefined) ?? 0),
  )
}

async function readCNFuturesReplayEvidence(path: string): Promise<CNFuturesReplayEvidence | undefined> {
  try {
    const payload = asRecord(await readOptionalJson(path)) as CNFuturesReplayPayload
    if (!Object.keys(payload).length) return undefined
    if (payload.real_trading_enabled === true) return undefined
    const summary = asRecord(payload.style_summary)
    let buyCount = 0
    let sellCount = 0
    let holdCount = 0
    const reasons = new Map<string, number>()
    for (const rawStyle of Object.values(summary)) {
      const style = asRecord(rawStyle)
      const actionCounts = asRecord(style.action_counts)
      buyCount += Math.max(0, Math.trunc(parseFiniteNumber(actionCounts.buy as number | string | undefined) ?? 0))
      sellCount += Math.max(0, Math.trunc(parseFiniteNumber(actionCounts.sell as number | string | undefined) ?? 0))
      holdCount += Math.max(0, Math.trunc(parseFiniteNumber(actionCounts.hold as number | string | undefined) ?? 0))
      const topReasons = asRecord(style.top_reasons)
      for (const [reason, value] of Object.entries(topReasons)) {
        reasons.set(reason, (reasons.get(reason) ?? 0) + Math.max(0, Math.trunc(parseFiniteNumber(value as number | string | undefined) ?? 0)))
      }
    }
    const examples = Array.isArray(payload.actionable_examples) ? payload.actionable_examples : []
    const firstExample = asRecord(examples[0])
    const executableCount = examples.filter((example) => asRecord(example).execution_eligible === true).length
    const nonExecutableReasons = new Map<string, number>()
    for (const example of examples) {
      const row = asRecord(example)
      if (row.execution_eligible === true) continue
      const reason = optionalString(row.execution_reason) ?? 'not_executable'
      nonExecutableReasons.set(reason, (nonExecutableReasons.get(reason) ?? 0) + 1)
    }
    return {
      generatedAt: String(payload.generated_at ?? ''),
      date: String(payload.date ?? ''),
      readOnly: payload.read_only === true,
      realTradingEnabled: false,
      symbolCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.symbol_count) ?? 0)),
      styleCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.style_count) ?? 0)),
      windowCount: Math.max(0, Math.trunc(parseFiniteNumber(payload.window_count) ?? 0)),
      buyCount,
      sellCount,
      holdCount,
      actionableCount: buyCount + sellCount,
      executableCount,
      nonExecutableReason: [...nonExecutableReasons.entries()].sort((left, right) => right[1] - left[1])[0]?.[0],
      topReason: [...reasons.entries()].sort((left, right) => right[1] - left[1])[0]?.[0],
      topSymbol: optionalString(firstExample.symbol),
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
    .filter((row) => !isDashboardExcluded(row as Record<string, unknown>))
    .map(parseEquitySnapshotRecord)
    .filter((row): row is ParsedEquitySnapshot => Boolean(row))
    .filter((row) => !(
      row.isSimLedgerSnapshot
      && [...row.markets].some((market) => market === 'A-share' || market === 'CNFutures')
    ))
    .sort((a, b) => a.timestampMs - b.timestampMs)

  if (!snapshots.length) return { performance: [] }

  const simLedgerSnapshots = snapshots.filter((snapshot) => snapshot.isSimLedgerSnapshot)
  if (hasAmbiguousEquityAuthorities(snapshots)) {
    return { performance: [] }
  }
  if (new Set(simLedgerSnapshots.map((snapshot) => dirname(snapshot.sourcePath))).size > 1) {
    return { performance: [] }
  }

  const grouped = snapshots.some((snapshot) => snapshot.isSimLedgerSnapshot)
    ? groupSimLedgerEquitySnapshots(simLedgerSnapshots)
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
      timestamp: row.timestamp,
      simulated: roundMetric(simulated),
      target: roundMetric(target),
      benchmark: roundMetric(row.benchmarkPct),
      opportunity: roundMetric(row.opportunityPct),
    }
  })
  const latest = grouped.get(timestamps.at(-1)!)!
  const latestCapitalBase = latest.capitalBase

  // DECOMMISSIONED: When multiple independent markets contribute equity
  // snapshots, do not produce a combined monetary portfolio summary or
  // cross-market performance curve. Per-market identity is in marketSummaries.
  const uniqueMarkets = [...latest.markets].filter((m) => m !== 'All Markets')
  if (uniqueMarkets.length > 1) {
    return { performance: [] }
  }

  return {
    performance,
    summary: {
      pnlAmount: roundMoney(latest.pnl),
      returnPct: roundMetric(latestCapitalBase > 0 ? (latest.pnl / latestCapitalBase) * 100 : latest.returnPct),
      capitalBase: roundMoney(latestCapitalBase),
      targetPct: latest.targetPct > 0 ? roundMetric(latest.targetPct) : DEFAULT_TARGET_RETURN_PCT,
      maxDrawdownPct: roundMetric(latest.maxDrawdownPct),
      tradeCount: latest.tradeCount,
      pointCount: performance.length,
      source: tradingAgentReadModelSources.equitySnapshots,
      pnlSource: latest.sources.size === 1 ? [...latest.sources][0] : latest.sources.size > 1 ? 'mixed' : 'equity_snapshot',
      pnlCurrency: latest.currency,
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
      grouped.set(snapshot.timestampMs, cloneEquitySnapshot(snapshot))
      continue
    }

    // Equity snapshots are full-account observations, never additive deltas.
    // Repeated observations from the sole validated authority replace the
    // prior row for that instant instead of doubling its capital and PnL.
    grouped.set(snapshot.timestampMs, cloneEquitySnapshot(snapshot))
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
  return { ...snapshot, markets: new Set(snapshot.markets), sources: new Set(snapshot.sources) }
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
  // DECOMMISSIONED: Cross-market monetary aggregation is forbidden.
  // capitalBase, pnl, realizedPnl, unrealizedPnl must never be summed
  // across independent markets. Only merge when markets overlap (same market).
  const sameMarket = [...current.markets].some((m) => snapshot.markets.has(m))
  if (!sameMarket) {
    // Cross-market: only merge non-monetary counts and market tracking.
    // maxDrawdownPct is per-market and must not be merged across markets.
    current.tradeCount += snapshot.tradeCount
    for (const market of snapshot.markets) current.markets.add(market)
    for (const source of snapshot.sources) current.sources.add(source)
    return
  }
  if (current.currency !== snapshot.currency || current.accountScope !== snapshot.accountScope || current.sourcePath !== snapshot.sourcePath) return

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
  for (const market of snapshot.markets) current.markets.add(market)
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

  const sourceMarket = marketFromEquitySourcePath(row.sourcePath)
  const currency = snapshotNativeCurrency(row, sourceMarket)
  if (!currency) return null
  const cryptoNative = sourceMarket === 'Crypto'
  const rawEquity = firstParsedNumber(row.total_equity, row.equity, row.nav, row.net_value, row.account_value, row.portfolio_value)
  const rawCapitalBase = firstParsedNumber(row.capital_base, row.initial_equity, row.starting_equity, row.start_equity, row.principal)
  const rawRealizedPnl = parseFiniteNumber(row.realized_pnl) ?? 0
  const rawUnrealizedPnl = parseFiniteNumber(row.unrealized_pnl) ?? 0
  const rawExplicitPnl = firstParsedNumber(row.pnl, row.total_pnl, row.net_pnl)
  const equity = cryptoNative
    ? rawEquity
    : firstParsedNumber(row.total_equity_cny, row.equity_cny) ?? rawEquity
  const capitalBase = cryptoNative
    ? rawCapitalBase
    : parseFiniteNumber(row.capital_base_cny) ?? rawCapitalBase
  const realizedPnl = cryptoNative
    ? rawRealizedPnl
    : parseFiniteNumber(row.realized_pnl_cny) ?? rawRealizedPnl
  const unrealizedPnl = cryptoNative
    ? rawUnrealizedPnl
    : parseFiniteNumber(row.unrealized_pnl_cny) ?? rawUnrealizedPnl
  const explicitPnl = cryptoNative
    ? rawExplicitPnl
    : firstParsedNumber(row.pnl_cny, row.total_pnl_cny, row.net_pnl_cny) ?? rawExplicitPnl
  const pnl = explicitPnl ?? (realizedPnl || unrealizedPnl ? realizedPnl + unrealizedPnl : equity !== undefined && capitalBase !== undefined ? equity - capitalBase : undefined)
  const returnPct = firstParsedNumber(row.simulated_return_pct, row.return_pct)

  if (pnl === undefined && returnPct === undefined) return null

  const base = capitalBase ?? (equity !== undefined && pnl !== undefined ? equity - pnl : undefined)
  if (base === undefined || base <= 0) return null

  const drawdownPct = firstParsedNumber(row.max_drawdown_pct, row.max_dd_pct)
  const drawdownAmount = firstParsedNumber(row.max_dd, row.drawdown)

  return {
    accountScope: firstString(row.account_scope, row.account_id, row.account) ?? `equity-source:${row.sourcePath}`,
    benchmarkPct: firstParsedNumber(row.benchmark_return_pct, row.benchmark_pct) ?? 0,
    capitalBase: base,
    currency,
    dayKey,
    isSimLedgerSnapshot: row.sourcePath.includes('/shared/logs/sim_ledger/'),
    markets: new Set([sourceMarket]),
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

function hasAmbiguousEquityAuthorities(snapshots: ParsedEquitySnapshot[]) {
  const authoritiesByMarket = new Map<Market, { accountScopes: Set<string>; sourcePaths: Set<string>; currencies: Set<string> }>()
  for (const snapshot of snapshots) {
    for (const market of snapshot.markets) {
      if (market === 'All Markets') return true
      const authority = authoritiesByMarket.get(market) ?? {
        accountScopes: new Set<string>(),
        sourcePaths: new Set<string>(),
        currencies: new Set<string>(),
      }
      authority.accountScopes.add(snapshot.accountScope)
      authority.sourcePaths.add(snapshot.sourcePath)
      authority.currencies.add(snapshot.currency)
      authoritiesByMarket.set(market, authority)
      if (authority.accountScopes.size > 1 || authority.sourcePaths.size > 1 || authority.currencies.size > 1) return true
    }
  }
  return false
}

function validatedMarketCapitalBase(
  market: Market,
  current?: number,
  freshAuthorityValidated = false,
) {
  if (market === 'All Markets') return current
  if (market === 'A-share' || market === 'CNFutures') {
    return freshAuthorityValidated && current !== undefined && current > 0
      ? current
      : undefined
  }
  return current !== undefined && current > 0 ? current : undefined
}

function marketFromEquitySourcePath(sourcePath: string): Market {
  const normalized = sourcePath.replaceAll('\\', '/')
  const simMatch = normalized.match(/\/shared\/logs\/sim_ledger\/([^/]+)\//)
  if (simMatch?.[1]) return normalizeMarketFolder(simMatch[1])
  const reviewMatch = normalized.match(/\/shared\/review\/([^/]+)\//)
  if (reviewMatch?.[1]) return normalizeMarketFolder(reviewMatch[1])
  return 'All Markets'
}

function parsePerformanceRow(row: PerformanceReviewRow): PerformancePoint | null {
  const day = formatReviewDay(row.trade_date ?? row.date ?? row.day)
  const simulated = firstNumber(row.simulated_return_pct, row.return_pct, row.pnl_pct, row.mtd_return_pct)
  if (!day || simulated === undefined) return null

  return {
    day,
    timestamp: row.trade_date ?? row.date ?? row.day,
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
      names.filter((name) => name.endsWith('.json')).map(async (name) => {
        const path = join(root, name)
        return parsePositionSnapshot(await readJson(path), `position-snapshot:${path}`)
      }),
    )
    return rows.flat().filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

async function readAuthoritativeASharePositions(
  projectRoot: string,
  capital: MarketCapitalProjection | undefined,
  now: Date,
): Promise<ASharePositionAuthorityRead> {
  if (!capital || capital.market !== 'A-share') return { holdings: [], state: 'not_applicable' }
  const lineageDir = executionLineageDir(projectRoot, capital.executionLineageId)
  if (!lineageDir) return { holdings: [], state: 'unavailable' }
  const path = join(lineageDir, 'simulated_ashare_positions.json')
  let payload: Record<string, unknown>
  try {
    payload = asRecord(JSON.parse((await readRegularFile(path)).toString('utf8')))
  } catch {
    const requiresReceipt = capital.openPositionCount > 0 || Math.abs(capital.positionsMarketValueCny) > 0.011
    return {
      holdings: [],
      state: requiresReceipt || await fileExists(path) ? 'unavailable' : 'empty',
    }
  }
  const scope = parseAShareProjectionAuthority(payload)
  const positionUpdatedAt = optionalString(payload.synced_at) ?? optionalString(payload.updated_at)
  if (
    !scope
    || !sameAShareCapitalAuthority(scope, capital)
    || payload.real_trading_enabled !== false
    || !positionUpdatedAt
    || !isFreshEvidenceInstant(positionUpdatedAt, now, MAX_POSITION_AUTHORITY_AGE_MS)
    || !Array.isArray(payload.positions)
    || !positionsMatchCapital(payload.positions, capital.positionsQuantityByRiskUnit)
  ) return { holdings: [], state: 'unavailable' }
  const holdings = parsePositionSnapshot(
    payload,
    `capital:${capital.authorityId}:${capital.authorityGeneration}:${capital.executionLineageId}`,
  ).filter((holding) => holding.market === 'A-share')
  if (holdings.length !== capital.openPositionCount) return { holdings: [], state: 'unavailable' }
  return { holdings, state: holdings.length ? 'ready' : 'empty' }
}

function isFreshEvidenceInstant(value: string, now: Date, maxAgeMs: number) {
  if (!isAwareIsoInstant(value)) return false
  const ageMs = now.getTime() - Date.parse(value)
  return ageMs >= 0 && ageMs <= maxAgeMs
}

function positionsMatchCapital(rawPositions: unknown[], expectedRaw: Record<string, number>) {
  const expected = new Map(
    Object.entries(expectedRaw).filter(([, quantity]) => quantity !== 0),
  )
  const actual = new Map<string, number>()
  for (const value of rawPositions) {
    const position = asRecord(value)
    const symbol = optionalString(position.ts_code)
    const quantity = parseFiniteNumber(position.quantity as number | string | undefined)
    if (!symbol || inferMarket(symbol) !== 'A-share' || quantity === undefined || !Number.isInteger(quantity) || quantity < 0) return false
    if (quantity === 0) continue
    if (actual.has(symbol)) return false
    actual.set(symbol, quantity)
  }
  if (actual.size !== expected.size) return false
  return [...expected].every(([symbol, quantity]) => actual.get(symbol) === quantity)
}

async function readPositionPlan(path: string): Promise<HoldingRow[]> {
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    const latest = JSON.parse(lines.at(-1) ?? '{}') as PositionPlanFile
    return (latest.positions ?? []).map((row) => parsePositionRow(
      row,
      'position_snapshot',
      `position-plan:${path}`,
    )).filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

async function readSimLedgerHoldings(root: string): Promise<HoldingRow[]> {
  const files = await listSimLedgerFiles(root, 'positions.json')
  const rows = await Promise.all(files.map(async (file) => {
    try {
      const payload = JSON.parse(await readFile(file.path, 'utf8')) as SimLedgerPositionsFile
      if (isDashboardExcluded(payload as Record<string, unknown>)) return []
      const accountScope = firstString(payload.account_scope, payload.account_id, payload.account)
        ?? `sim-ledger:${file.market}:${file.strategy}`
      return Object.entries(payload.positions ?? {}).map(([symbol, position]) => parseSimLedgerPosition(
        symbol,
        position,
        file.market,
        file.strategy,
        accountScope,
      ))
    } catch {
      return []
    }
  }))

  return rows.flat().filter((row): row is HoldingRow => Boolean(row))
}

function parseSimLedgerPosition(
  symbol: string,
  position: SimLedgerPosition,
  marketHint: string,
  strategy: string,
  accountScope: string,
): HoldingRow | null {
  if (!symbol || !position.quantity) return null
  const market = normalizeMarket(marketHint, symbol)
  if (market === 'All Markets') return null
  const cost = roundMoney(Number(position.avg_cost ?? 0) * Number(position.quantity ?? 0))
  const realizedPnl = roundMoney(position.realized_pnl ?? 0)
  const unrealizedPnl = position.unrealized_pnl === undefined ? undefined : roundMoney(position.unrealized_pnl)
  const averagePrice = roundMoney(Number(position.avg_cost ?? 0))

  return {
    symbol: normalizeSymbol(symbol, market),
    name: normalizeSymbol(symbol, market),
    market,
    opportunityId: explicitOpportunityId(position),
    marketDataSymbol: explicitMarketDataSymbol(position),
    weight: formatMarketCurrencyAmount(cost, market),
    pnl: formatMarketCurrency(realizedPnl + (unrealizedPnl ?? 0), market),
    risk: position.quantity > 0 ? '正常' : '观察',
    role: `${formatStrategyName(strategy)} 持仓`,
    quantity: Number(position.quantity),
    averagePrice,
    costBasis: cost,
    marketValue: cost,
    dayPnl: realizedPnl + (unrealizedPnl ?? 0),
    realizedPnl,
    unrealizedPnl,
    currency: marketNativeCurrency(market),
    accountScope,
    source: 'sim_ledger',
  }
}

function parsePositionSnapshot(payload: unknown, accountScope?: string): HoldingRow[] {
  const payloadRecord = asRecord(payload)
  const effectiveAccountScope = firstString(
    payloadRecord.account_scope,
    payloadRecord.account_id,
    payloadRecord.account,
  ) ?? accountScope
  if (Array.isArray(payload)) {
    return payload.map((row) => parsePositionRow(row, 'position_snapshot', effectiveAccountScope)).filter((row): row is HoldingRow => Boolean(row))
  }

  const direct = parsePositionRow(payload, 'position_snapshot', effectiveAccountScope)
  if (direct) return [direct]

  const snapshot = payload as CNFuturesPositionsFile
  if (Array.isArray(snapshot.positions)) {
    return snapshot.positions
      .map((row) => (asRecord(row).ts_code
        ? parsePositionRow(row, 'position_snapshot', effectiveAccountScope)
        : parseCNFuturesPositionRow(row, effectiveAccountScope)))
      .filter((row): row is HoldingRow => Boolean(row))
  }

  return []
}

function parseCNFuturesPositionRow(row: CNFuturesPositionRow, accountScope?: string): HoldingRow | null {
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
    opportunityId: explicitOpportunityId(row),
    marketDataSymbol: explicitMarketDataSymbol(row),
    weight: margin === undefined ? `${qty} 手` : formatCurrency(margin),
    pnl: formatCurrency(realized + unrealized),
    risk: qty > 0 ? '正常' : '观察',
    role: `${style} 持仓`,
    quantity: qty,
    averagePrice: parseFiniteNumber(row.avg_price),
    markPrice: parseFiniteNumber(row.mark_price),
    costBasis: parseFiniteNumber(row.avg_price) === undefined ? undefined : Math.abs(qty) * Number(row.avg_price),
    marketValue: margin,
    dayPnl: realized + unrealized,
    realizedPnl: realized,
    unrealizedPnl: unrealized,
    currency: 'CNY',
    accountScope,
    source: 'position_snapshot',
  }
}

function parsePositionRow(
  row: unknown,
  source: HoldingRow['source'] = 'position_snapshot',
  sourceAccountScope?: string,
): HoldingRow | null {
  const position = row as PositionRow
  const symbol = position.ts_code
  if (!symbol) return null
  const market = inferMarket(symbol)
  if (market === 'All Markets') return null
  const marketValue = parseFiniteNumber(position.market_value)
  const runningCost = firstParsedNumber(position.cost_basis, position.running_cost)
  const quantity = parseFiniteNumber(position.quantity)
  const realizedPnl = parseFiniteNumber(position.realized_pnl) ?? 0
  const unrealizedPnl = parseFiniteNumber(position.unrealized_pnl)
  const pnl = firstParsedNumber(position.pnl, unrealizedPnl === undefined ? undefined : realizedPnl + unrealizedPnl, position.realized_pnl) ?? 0
  const averagePrice = firstParsedNumber(position.avg_price, position.running_avg_price)
  const markPrice = firstParsedNumber(position.price, marketValue !== undefined && quantity ? marketValue / quantity : undefined)

  return {
    symbol: normalizeSymbol(symbol, market),
    name: normalizeSymbol(symbol, market),
    market,
    opportunityId: explicitOpportunityId(position),
    marketDataSymbol: explicitMarketDataSymbol(position),
    weight: formatMarketCost(marketValue ?? runningCost ?? quantity, market),
    pnl: formatMarketCurrency(pnl, market),
    risk: '正常',
    role: position.thesis ?? (position.side ? `${position.side} 持仓` : '模拟盘持仓'),
    quantity,
    averagePrice,
    markPrice,
    costBasis: runningCost,
    marketValue,
    dayPnl: pnl,
    realizedPnl,
    unrealizedPnl,
    currency: marketNativeCurrency(market),
    accountScope: firstString(position.account_scope, position.account_id, position.account) ?? sourceAccountScope,
    updatedAt: position.entry_date,
    source,
  }
}

function explicitOpportunityId(row: { opportunity_id?: string | number; opportunityId?: string | number; signal_id?: string | number; trace_id?: string | number; order_id?: string | number }) {
  return firstString(row.opportunity_id, row.opportunityId, row.signal_id, row.trace_id, row.order_id)
}

function explicitMarketDataSymbol(row: { market_data_symbol?: string; marketDataSymbol?: string; metadata?: Record<string, unknown> }) {
  return firstString(row.market_data_symbol, row.marketDataSymbol, row.metadata?.market_data_symbol, row.metadata?.marketDataSymbol)
}

async function readSimLedgerSignals(root: string, now: Date): Promise<SignalRow[]> {
  const files = await listSimLedgerFiles(root, 'trade_journal.jsonl')
  const rows = await Promise.all(files.map(async (file) => {
    try {
      const lines = (await readFile(file.path, 'utf8')).trim().split('\n').filter(Boolean)
      return lines.slice(-MAX_SIM_LEDGER_SIGNALS).map((line) => {
        try {
          const trade = JSON.parse(line) as SimLedgerTradeRow
          if (isDashboardExcluded(trade as Record<string, unknown>)) return null
          return parseSimLedgerTrade(trade, file.market, file.strategy, now)
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
    .filter(dedupeSimLedgerSignal())
    .slice(0, MAX_SIM_LEDGER_SIGNALS)
}

function dedupeSimLedgerSignal() {
  const seen = new Set<string>()
  return (row: SignalRow) => {
    const key = row.opportunityId
      ? `opportunity:${row.opportunityId}`
      : `${row.market}:${row.symbol}:${row.status}:${row.stage}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  }
}

function parseSimLedgerTrade(row: SimLedgerTradeRow, marketHint: string, strategy: string, now: Date): SignalRow | null {
  if (!row.symbol) return null
  const market = normalizeMarket(marketHint, row.symbol)
  if (market === 'All Markets') return null
  const symbol = normalizeSymbol(row.symbol, market)
  const timestamp = row.timestamp
  const canonicalStrategy = row.strategy_name || strategy
  const opportunityId = getSimLedgerOpportunityId(row, symbol, market, canonicalStrategy, timestamp)

  return {
    symbol,
    name: symbol,
    market,
    opportunityId,
    queueBucket: 'filled',
    method: `${formatStrategyName(canonicalStrategy)} · ${row.side === 'sell' ? '卖出' : '买入'}`,
    strategyName: row.strategy_name,
    signalSource: row.signal_source,
    status: 'executed',
    impact: row.notional === undefined ? '--' : `成交 ${formatMarketCurrencyAmount(row.notional, market)}`,
    confidence: formatConviction(row.conviction) ?? '已成交',
    age: formatAge(timestamp, now),
    reason: row.reason ?? `模拟盘已按 ${formatPrice(row.fill_price)} 成交`,
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

function getSimLedgerOpportunityId(row: SimLedgerTradeRow, symbol: string, market: string, strategy: string, timestamp?: string) {
  return firstString(
    row.opportunity_id,
    row.signal_id,
    row.trace_id,
    row.order_id,
    row.card_id,
    row.metadata?.opportunity_id,
    row.metadata?.signal_id,
    row.metadata?.trace_id,
    row.metadata?.order_id,
  ) ?? `sim_ledger:${market}:${symbol}:${strategy}:${timestamp ?? 'unknown'}`
}

function formatConviction(value: number | string | undefined) {
  const parsed = parseFiniteNumber(value)
  if (parsed === undefined) return undefined
  const ratio = parsed <= 1 ? parsed * 100 : parsed
  if (!Number.isFinite(ratio)) return undefined
  return `${Math.round(ratio)}%`
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
      if (normalizeMarketFolder(market.name) === 'All Markets') continue
      const marketRoot = join(root, market.name)
      const strategies = await readdir(marketRoot, { withFileTypes: true })
      for (const strategy of strategies) {
        if (!strategy.isDirectory()) continue
        if (market.name.toLowerCase() === 'ashare' && strategy.name !== 'ashare_sim') continue
        const strategyRoot = join(marketRoot, strategy.name)
        if (targetName !== 'positions.json' && await isSimLedgerStrategyExcluded(strategyRoot)) continue
        const path = join(strategyRoot, targetName)
        if (await fileExists(path)) files.push({ path, market: market.name, strategy: strategy.name })
      }
    }
  } catch {
    return []
  }
  return files
}

async function isSimLedgerStrategyExcluded(strategyRoot: string) {
  const payload = asRecord(await readOptionalJson(join(strategyRoot, 'positions.json')))
  if (!Object.keys(payload).length) return false
  return isDashboardExcluded(payload)
}

async function readSignalFile(path: string, bucket: string, now: Date): Promise<SignalRow | null> {
  try {
    const raw = JSON.parse(await readFile(path, 'utf8')) as SignalFile
    const rawSymbol = raw.ts_code ?? raw.symbol
    if (!rawSymbol) return null
    const market = normalizeMarket(raw.market, rawSymbol)
    if (market === 'All Markets') return null
    const symbol = normalizeSymbol(rawSymbol, market)
    if (!isSignalQueueRowVisible(raw, market)) return null
    const status = mapSignalStatus(raw.status ?? bucket, raw)
    const stage = inferSignalStage(raw, bucket)
    const stageTimes = formatStageTimes(raw)
    const opportunityId = getSignalOpportunityId(raw, symbol, market, bucket, path)

    return {
      symbol,
      name: symbol,
      market,
      opportunityId,
      marketDataSymbol: explicitMarketDataSymbol(raw),
      queueBucket: bucket,
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
      capitalEvidence: extractSignalCapitalEvidence(raw),
    }
  } catch {
    return null
  }
}

function getSignalOpportunityId(signal: SignalFile, symbol: string, market: Market, bucket: string, path: string) {
  const explicitId = firstString(
    signal.opportunity_id,
    signal.signal_id,
    signal.trace_id,
    signal.id,
    signal.card_id,
    signal.order_id,
  )
  if (explicitId) return explicitId
  const fileId = basename(path, '.json') || symbol
  return `${market}:${symbol}:${bucket}:${fileId}`
}

function extractSignalCapitalEvidence(raw: SignalFile): SignalCapitalEvidence | undefined {
  const scores = firstRecord(raw.scores, raw.score, raw.dimension_scores, raw.factor_scores)
  const score = firstParsedNumber(raw.capital_score, raw.moneyflow_score, scores.capital, scores.moneyflow)
  const netInflow = firstParsedNumber(raw.net_mf_amount, raw.main_net_inflow, scores.net_mf_amount, scores.main_net_inflow, scores.moneyflow)
  const largeOrderNetInflow = firstParsedNumber(raw.large_order_net_inflow, scores.large_order_net_inflow, scores.buy_lg_amount, scores.net_lg_amount)
  const superLargeOrderNetInflow = firstParsedNumber(raw.super_large_order_net_inflow, scores.super_large_order_net_inflow, scores.buy_elg_amount, scores.net_elg_amount)

  if (
    score === undefined &&
    netInflow === undefined &&
    largeOrderNetInflow === undefined &&
    superLargeOrderNetInflow === undefined
  ) {
    return undefined
  }

  return {
    score: score === undefined ? undefined : roundMetric(score),
    netInflow,
    mainNetInflow: netInflow,
    largeOrderNetInflow,
    superLargeOrderNetInflow,
    source: 'signal_scores',
  }
}

function isSignalQueueRowVisible(signal: SignalFile, market: Market) {
  if (market !== 'A-share') return true
  const rawStatus = String(signal.status ?? '').toLowerCase()
  const direction = String(signal.direction ?? signal.side ?? '').toLowerCase()
  const isExecutionState = ['filled', 'executed', 'failed', 'partial', 'cancelled', 'expired'].includes(rawStatus)
    || signal.fill !== undefined
    || signal.simulated_fill !== undefined
    || signal.filled_at !== undefined
  // `signals/pending` is a retired compatibility queue, not the V1 decision
  // authority.  Until an authority-bound read-only projection exists, an
  // A-share pending row cannot drive current/live dashboard state.
  if (!isExecutionState) return false
  if (direction === 'sell') return String(signal.execution_source ?? '').toLowerCase() === 'ashare_rebalance_sell'
  return String(signal.candidate_pool_layer ?? '').toLowerCase() === 'candidate'
    && String(signal.execution_source ?? '').toLowerCase() === 'ashare_candidate_layer'
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
  if (symbol.endsWith('.SH') || symbol.endsWith('.SZ')) return 'A-share'
  if (/\.(CFFEX|CFE|CFX|SHFE|SHF|DCE|CZCE|INE|GFEX)$/i.test(symbol)) return 'CNFutures'
  if (isActiveCryptoSymbol(symbol)) return 'Crypto'
  return 'All Markets'
}

function normalizeMarket(market: string | undefined, symbol: string): Market {
  const value = market?.toLowerCase()
  if (value === 'a-share' || value === 'ashare' || value === 'a_share' || value === 'cn') return 'A-share'
  if (value === 'crypto') return isActiveCryptoSymbol(symbol) ? 'Crypto' : 'All Markets'
  if (value === 'cn_futures' || value === 'cnfutures' || value === 'futures' || value === 'china-futures' || value === 'china_futures') return 'CNFutures'
  return inferMarket(symbol)
}

function normalizeSymbol(symbol: string, market: Market) {
  if (market === 'Crypto' && isActiveCryptoSymbol(symbol)) {
    const compact = symbol.trim().toUpperCase().replace('-', '')
    return `${compact.slice(0, -4)}-USDT`
  }
  return symbol
}

function isActiveCryptoSymbol(symbol: string) {
  return /^[A-Z0-9]{2,20}-?USDT$/.test(symbol.trim().toUpperCase())
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

async function readOpportunityFunnelEvents(projectRoot: string): Promise<FunnelEvent[]> {
  const candidatePaths = [
    join(projectRoot, 'shared/review/opportunities/funnel_events.jsonl'),
    join(projectRoot, 'shared/logs/opportunities/funnel_events.jsonl'),
  ]
  const events: FunnelEvent[] = []

  for (const path of candidatePaths) {
    try {
      const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
      lines.slice(-MAX_OPPORTUNITY_FUNNEL_EVENTS).forEach((line, index) => {
        try {
          const parsed = parseOpportunityFunnelEvent(JSON.parse(line) as OpportunityFunnelEventRow, index)
          if (parsed) events.push(parsed)
        } catch {
          // Ignore malformed event rows; valid event rows remain usable.
        }
      })
    } catch {
      // Retired opportunity-writer logs are optional frozen forensic history.
    }
  }

  return events.sort((a, b) => (a.sequence ?? opportunityStageSequence(a.stage)) - (b.sequence ?? opportunityStageSequence(b.stage)))
}

function parseOpportunityFunnelEvent(row: OpportunityFunnelEventRow, index: number): FunnelEvent | null {
  const metadata = asRecord(row.metadata)
  const rawSymbol = firstString(row.ts_code, row.symbol, metadata.ts_code, metadata.symbol)
  if (!rawSymbol) return null
  const market = normalizeMarket(firstString(row.market, metadata.market), rawSymbol)
  if (market === 'All Markets') return null
  const symbol = normalizeSymbol(rawSymbol, market)
  const stage = mapOpportunityFunnelStage(firstString(row.stage, metadata.stage, row.status, metadata.status))
  const status = mapOpportunityFunnelStatus(firstString(row.status, metadata.status), stage)
  const sequence = parseFiniteNumber(row.sequence) ?? parseFiniteNumber(metadata.sequence as number | string | undefined) ?? opportunityStageSequence(stage)
  const timestamp = firstString(row.at, row.timestamp, row.ts, row.created_at, row.updated_at, metadata.at, metadata.timestamp, metadata.created_at, metadata.updated_at)
  const opportunityId = firstString(
    row.opportunity_id,
    row.opportunityId,
    row.signal_id,
    row.trace_id,
    row.order_id,
    row.card_id,
    metadata.opportunity_id,
    metadata.opportunityId,
    metadata.signal_id,
    metadata.trace_id,
    metadata.order_id,
    metadata.card_id,
  ) ?? `${market}:${symbol}:opportunity:${index}`
  const label = firstString(row.label, metadata.label) ?? defaultOpportunityEventLabel(stage, status)
  const explicitTerminal = row.terminal === undefined ? metadata.terminal : row.terminal

  return {
    id: firstString(row.event_id, row.id, metadata.event_id, metadata.id) ?? `legacy_frozen_opportunity_log:${opportunityId}:${sequence}:${index}`,
    symbol,
    market,
    opportunityId,
    sequence,
    stage,
    status,
    label,
    at: formatClock(timestamp),
    source: 'legacy_frozen_opportunity_log',
    reason: firstString(row.reason, metadata.reason),
    latencyMinutes: parseFiniteNumber(row.latencyMinutes ?? row.latency_minutes) ?? parseFiniteNumber(metadata.latencyMinutes as number | string | undefined) ?? parseFiniteNumber(metadata.latency_minutes as number | string | undefined),
    terminal: explicitTerminal === undefined ? stage === '结果' || status === '成交' || status === '拦截' || status === '复盘' : boolish(explicitTerminal),
  }
}

function mergeFunnelEvents(primaryEvents: FunnelEvent[], derivedEvents: FunnelEvent[]) {
  const rows = [...primaryEvents, ...derivedEvents]
  const seen = new Set<string>()
  return rows
    .filter((event) => {
      const key = `${event.source}:${event.opportunityId ?? event.id}:${event.stage}:${event.status}:${event.at ?? ''}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    .sort((a, b) => {
      const group = String(a.opportunityId ?? a.id).localeCompare(String(b.opportunityId ?? b.id))
      if (group !== 0) return group
      return (a.sequence ?? opportunityStageSequence(a.stage)) - (b.sequence ?? opportunityStageSequence(b.stage))
    })
}

function mapOpportunityFunnelStage(value: string | undefined): FunnelEvent['stage'] {
  const normalized = normalizeEventToken(value)
  if (/result|outcome|filled|executed|done|missed|rejected|cancelled|canceled|blocked|expired|结果|成交|复盘|拦截/.test(normalized)) return '结果'
  if (/pending|queue|queued|waiting|trigger|triggered|ready|confirm|待确认|待执行|等待/.test(normalized)) return '待确认'
  if (/risk|gate|checked|风控/.test(normalized)) return '风控'
  if (/research|score|scored|debate|debated|thesis|judge|研判|研究|评分/.test(normalized)) return '研判'
  return '发现'
}

function mapOpportunityFunnelStatus(value: string | undefined, stage: FunnelEvent['stage']): FunnelEventStatus {
  const normalized = normalizeEventToken(value)
  if (/filled|executed|done|成交|已兑现/.test(normalized)) return '成交'
  if (/blocked|rejected|risk|deny|denied|拦截|拒绝/.test(normalized)) return '拦截'
  if (/missed|expired|cancelled|canceled|review|复盘|错过|取消/.test(normalized)) return '复盘'
  if (/pending|queue|queued|waiting|trigger|confirm|待确认|等待/.test(normalized)) return '等待'
  if (/discover|found|scan|opportunity|机会|发现/.test(normalized)) return '进入'
  if (stage === '发现') return '进入'
  if (stage === '待确认') return '等待'
  return '通过'
}

function defaultOpportunityEventLabel(stage: FunnelEvent['stage'], status: FunnelEventStatus) {
  if (status === '成交') return '结果兑现'
  if (status === '拦截') return '风险挡住'
  if (status === '复盘') return '进入复盘'
  if (stage === '发现') return '发现机会'
  if (stage === '研判') return '形成判断'
  if (stage === '风控') return '风控检查'
  if (stage === '待确认') return '等待确认'
  return '结果更新'
}

function normalizeEventToken(value: string | undefined) {
  return String(value ?? '').trim().toLowerCase().replace(/[\s_-]+/g, '')
}

function opportunityStageSequence(stage: FunnelEvent['stage']) {
  if (stage === '结果') return 5
  if (stage === '待确认') return 4
  if (stage === '风控') return 3
  if (stage === '研判') return 2
  return 1
}

function buildFunnelEvents(signals: SignalRow[]): FunnelEvent[] {
  return signals.flatMap((signal, index) => {
    const source: FunnelEvent['source'] = signal.stageEvidence === 'replay' ? 'sim_ledger' : 'signal_queue'
    const rank = eventStageRank(signal)
    const opportunityId = signal.opportunityId ?? `${source}:${signal.market}:${signal.symbol}:${index}`
    const baseId = `${source}-${opportunityId}`
    const eventBase = {
      latencyMinutes: signal.stageLatencyMinutes,
      market: signal.market,
      opportunityId,
      source,
      symbol: signal.symbol,
    }
    const events: FunnelEvent[] = [
      {
        ...eventBase,
        id: `${baseId}-discover`,
        sequence: 1,
        stage: '发现',
        status: '进入',
        label: '发现机会',
        at: signal.stageTimes?.discovered,
        reason: signal.reason,
      },
    ]

    if (rank >= 1 || signal.stageTimes?.scored || signal.stageTimes?.debated) {
      events.push({
        ...eventBase,
        id: `${baseId}-research`,
        sequence: 2,
        stage: '研判',
        status: '通过',
        label: '形成判断',
        at: signal.stageTimes?.debated ?? signal.stageTimes?.scored,
        reason: signal.method,
      })
    }

    if (rank >= 2 || signal.status === 'blocked') {
      events.push({
        ...eventBase,
        id: `${baseId}-risk`,
        sequence: 3,
        stage: '风控',
        status: signal.status === 'blocked' ? '拦截' : '通过',
        label: signal.status === 'blocked' ? '风险拦截' : '风控通过',
        at: signal.stageTimes?.riskChecked,
        reason: signal.reason,
        terminal: signal.status === 'blocked',
      })
    }

    if (rank >= 3 && signal.status !== 'blocked') {
      events.push({
        ...eventBase,
        id: `${baseId}-queue`,
        sequence: 4,
        stage: '待确认',
        status: signal.status === 'pending' ? '等待' : '通过',
        label: signal.status === 'pending' ? '等待触发' : '进入结果',
        at: signal.stageTimes?.triggered,
        reason: signal.next,
      })
    }

    if (rank >= 4 || signal.status !== 'pending') {
      events.push({
        ...eventBase,
        id: `${baseId}-result`,
        sequence: 5,
        stage: '结果',
        status: eventResultStatus(signal),
        label: eventResultLabel(signal),
        at: signal.stageTimes?.triggered,
        reason: signal.impact,
        terminal: true,
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
  if (signal.status === 'missed') return '复盘'
  if (signal.status === 'blocked') return '拦截'
  if (signal.status === 'cancelled') return '复盘'
  return '等待'
}

function eventResultLabel(signal: SignalRow) {
  if (signal.status === 'executed') return '已兑现'
  if (signal.status === 'missed') return '纳入复盘'
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

function firstString(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  }
  return undefined
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

function formatShanghaiDateKey(date: Date) {
  const parts = new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    timeZone: 'Asia/Shanghai',
  }).formatToParts(date)
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}${part('month')}${part('day')}`
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

function firstRecord(...values: unknown[]): Record<string, unknown> {
  return values.map(asRecord).find((value) => Object.keys(value).length > 0) ?? {}
}

function optionalString(value: unknown) {
  return value === undefined || value === null || value === '' ? undefined : String(value)
}

function firstParsedNumber(...values: unknown[]) {
  return values.map((value) => parseFiniteNumber(value as number | string | undefined)).find((value): value is number => value !== undefined)
}

function boolish(value: unknown) {
  if (value === true) return true
  if (typeof value !== 'string') return false
  return ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase())
}

function isDashboardExcluded(row: Record<string, unknown>) {
  if (
    boolish(row.exclude_from_dashboard)
    || boolish(row.excluded_from_dashboard)
    || boolish(row.dashboard_excluded)
    || boolish(row.maintenance_run)
  ) {
    return true
  }

  const metadata = asRecord(row.metadata)
  if (Object.keys(metadata).length && isDashboardExcluded(metadata)) return true

  const scope = [
    row.run_context,
    row.run_mode,
    row.run_source,
    row.sample_type,
  ]
    .map((value) => String(value ?? '').trim().toLowerCase())
    .filter(Boolean)
    .join(' ')

  return /\b(maintenance|backfill|smoke|repair|bootstrap|dry[-_ ]?run)\b/.test(scope)
}

function parseSnapshotTimestamp(value: string) {
  const direct = Date.parse(value)
  if (Number.isFinite(direct)) return direct
  const compact = /^(\d{4})(\d{2})(\d{2})(?:[T_ -]?(\d{2})(\d{2})(\d{2})?)?/.exec(value)
  if (!compact) return Number.NaN
  const [, year, month, day, hour = '00', minute = '00', second = '00'] = compact
  return Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}+08:00`)
}

function marketNativeCurrency(market: Market): NonNullable<MarketSummary['pnlCurrency']> {
  return market === 'Crypto' ? 'USDT' : 'CNY'
}

function snapshotNativeCurrency(
  row: EquitySnapshotRecord,
  market: Market,
): NonNullable<MarketSummary['pnlCurrency']> | undefined {
  if (market === 'All Markets') return undefined
  const expected = marketNativeCurrency(market)
  const declared = String(row.display_currency ?? row.currency ?? '').trim().toUpperCase()
  if (declared && declared !== expected) return undefined
  return expected
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

function normalizeCapitalLayer(row: EquitySnapshotRow) {
  return String(row.capital_layer ?? row.account_type ?? 'simulated').toLowerCase()
}

function formatCurrency(value: number) {
  return formatCny(value, true)
}

function formatMarketCost(value: number | undefined, market: HoldingRow['market']) {
  if (value === undefined) return '--'
  return formatMarketCurrencyAmount(value, market)
}

function formatMarketCurrency(value: number, _market: HoldingRow['market']) {
  return _market === 'Crypto' ? formatUsdt(value, true) : formatCny(value, true)
}

function formatMarketCurrencyAmount(value: number, _market: HoldingRow['market']) {
  return _market === 'Crypto' ? formatUsdt(value, false) : formatCny(value, false)
}

function formatUsdt(value: number, signed: boolean) {
  const prefix = value > 0 && signed ? '+' : value < 0 ? '-' : ''
  return `${prefix}${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 2 })} USDT`
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
