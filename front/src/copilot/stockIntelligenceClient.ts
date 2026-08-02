import { assessForecastReadiness, forecastHorizons } from './forecastReadiness.ts'
import type { StockIntelligence } from './stockIntelligence.ts'

export const TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE = '/api/trading-copilot/stock-intelligence'

export async function loadStockIntelligence(symbol: string, fetcher: typeof fetch = fetch): Promise<StockIntelligence | null> {
  try {
    const response = await fetcher(`${TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE}?symbol=${encodeURIComponent(symbol)}`, {
      method: 'GET', headers: { Accept: 'application/json' },
    })
    if (!response.ok) return null
    const payload = await response.json()
    assertStockIntelligenceProjection(payload, symbol)
    return payload
  } catch {
    return null
  }
}

export function assertStockIntelligenceProjection(payload: unknown, expectedSymbol?: string): asserts payload is StockIntelligence {
  if (!payload || typeof payload !== 'object') throw new Error('stock_intelligence_invalid')
  const value = payload as Partial<StockIntelligence>
  if (!value.symbol || value.symbol !== value.symbol.toUpperCase() || (expectedSymbol && value.symbol !== expectedSymbol)) throw new Error('stock_intelligence_symbol_invalid')
  if (value.mode !== 'tradingagent_observation' || !isTimestamp(value.updatedAt) || !value.quote || !value.company || !value.series || !Array.isArray(value.events)) throw new Error('stock_intelligence_formal_projection_required')
  if (value.verification?.status !== 'verified' || !value.verification.receiptId || !isSha256(value.verification.projectionSha256) || !isTimestamp(value.verification.validUntil) || Date.parse(value.verification.validUntil) <= Date.now() || !isTimestamp(value.verification.verifiedAt) || !value.verification.verifierId) throw new Error('stock_intelligence_detached_verification_required')
  assertProjectionSource(value.source)
  assertMarketRules(value.marketRules)
  assertQuote(value.quote)
  assertAnalysis(value.analysis, value.symbol)
  if (!Object.values(value.series).every((points) => Array.isArray(points) && points.every(assertSeriesPoint))) throw new Error('stock_intelligence_series_invalid')
  if (value.events.some((event) => !event.relatedSymbols.includes(value.symbol!) || !isTimestamp(event.publishedAt) || !isTimestamp(event.retrievedAt) || !event.url || !isSha256(event.sourceReceiptSha256) || !event.sourceReceiptId || !isSha256(event.contentSha256))) throw new Error('stock_intelligence_event_binding_invalid')
  if (value.forecast) {
    const readiness = value.forecast.readiness
    const recomputed = assessForecastReadiness(value.forecast.evidence)
    if (readiness.status === 'illustrative_only') throw new Error('stock_intelligence_formal_forecast_cannot_be_illustrative')
    if (!forecastHorizons.includes(value.forecast.horizon) || readiness.horizon !== value.forecast.horizon || readiness.modelId !== value.forecast.modelId) throw new Error('stock_intelligence_forecast_binding_invalid')
    if (JSON.stringify(readiness) !== JSON.stringify(recomputed)) throw new Error('stock_intelligence_forecast_readiness_not_recomputed')
    if (readiness.status === 'decision_support_ready' && (!readiness.gates.every((gate) => gate.passed) || !readiness.probabilitiesVisible || !readiness.intervalsMayUseCoverageLabels)) throw new Error('stock_intelligence_forecast_readiness_invalid')
    if (readiness.status !== 'decision_support_ready' && (readiness.probabilitiesVisible || readiness.intervalsMayUseCoverageLabels)) throw new Error('stock_intelligence_forecast_probability_leak')
  }
  if (value.analysis?.readiness.action === 'eligible_for_human_review') {
    if (value.source?.freshness !== 'fresh' || value.marketRules?.tradingStatus !== 'trading' || value.analysis.evidenceStrength.semantics !== 'typed_evidence_strength_v1') throw new Error('stock_intelligence_action_readiness_invalid')
  }
}

function assertProjectionSource(source: StockIntelligence['source'] | undefined) {
  if (!source || !source.datasetId || !source.receiptId || !isSha256(source.receiptSha256) || !isTimestamp(source.dataThrough) || !isTimestamp(source.retrievedAt) || !['fresh', 'stale', 'degraded'].includes(source.freshness)) throw new Error('stock_intelligence_source_invalid')
}

function assertMarketRules(rules: StockIntelligence['marketRules'] | undefined) {
  if (!rules || rules.lotSize !== 100 || rules.tPlusOne !== true || !['main', 'gem', 'star', 'beijing', 'unknown'].includes(rules.board) || !['normal', 'st', 'star_st', 'unknown'].includes(rules.stStatus) || !['trading', 'suspended', 'unknown'].includes(rules.tradingStatus)) throw new Error('stock_intelligence_market_rules_invalid')
  if (rules.priceLimitPct !== null && (!Number.isFinite(rules.priceLimitPct) || rules.priceLimitPct <= 0)) throw new Error('stock_intelligence_price_limit_invalid')
}

function assertQuote(quote: NonNullable<StockIntelligence['quote']>) {
  const numbers = [quote.price, quote.previousClose, quote.change, quote.changePct, quote.open, quote.high, quote.low, quote.volume]
  if (numbers.some((number) => !Number.isFinite(number)) || quote.price <= 0 || quote.previousClose <= 0 || quote.high < quote.low || quote.volume < 0) throw new Error('stock_intelligence_quote_invalid')
  for (const optional of [quote.turnoverRate, quote.peTtm, quote.marketCapCny]) {
    if (optional !== null && !Number.isFinite(optional)) throw new Error('stock_intelligence_quote_invalid')
  }
  if ((quote.turnoverRate !== null && quote.turnoverRate < 0) || (quote.marketCapCny !== null && quote.marketCapCny < 0)) throw new Error('stock_intelligence_quote_invalid')
}

function assertAnalysis(analysis: StockIntelligence['analysis'] | undefined, symbol: string) {
  if (!analysis || analysis.symbol !== symbol || analysis.mode !== 'tradingagent_observation') throw new Error('stock_intelligence_analysis_invalid')
  const strength = analysis.evidenceStrength
  if (strength.semantics !== 'typed_evidence_strength_v1' || strength.contractVersion !== 'v1' || strength.value === null || !Number.isFinite(strength.value) || strength.value < 0 || strength.value > 100 || !strength.sourceRefs.length || !isTimestamp(strength.asOf)) throw new Error('stock_intelligence_evidence_strength_invalid')
  if (analysis.readiness.data !== 'verified' || analysis.readiness.evidence !== 'typed' || !analysis.readiness.reasons.length) throw new Error('stock_intelligence_analysis_readiness_invalid')
}

function assertSeriesPoint(point: unknown) {
  if (!point || typeof point !== 'object') return false
  const value = point as Record<string, unknown>
  if (typeof value.key !== 'string' || typeof value.label !== 'string') return false
  for (const field of ['price', 'volume', 'forecastMedian']) {
    const number = value[field]
    if (number !== null && (typeof number !== 'number' || !Number.isFinite(number))) return false
  }
  for (const field of ['forecastNarrowEnvelope', 'forecastWideEnvelope']) {
    const envelope = value[field]
    if (envelope !== null && (!Array.isArray(envelope) || envelope.length !== 2 || envelope.some((item) => typeof item !== 'number' || !Number.isFinite(item)) || envelope[0] > envelope[1])) return false
  }
  return true
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
}
