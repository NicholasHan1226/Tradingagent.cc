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
  if (value.mode !== 'tradingagent_observation' || !value.updatedAt || !value.quote || !value.company || !value.series || !Array.isArray(value.events)) throw new Error('stock_intelligence_formal_projection_required')
  if (!Object.values(value.series).every(Array.isArray)) throw new Error('stock_intelligence_series_invalid')
  if (value.events.some((event) => !event.relatedSymbols.includes(value.symbol!))) throw new Error('stock_intelligence_event_binding_invalid')
  if (value.forecast) {
    const readiness = value.forecast.readiness
    const recomputed = assessForecastReadiness(value.forecast.evidence)
    if (readiness.status === 'illustrative_only') throw new Error('stock_intelligence_formal_forecast_cannot_be_illustrative')
    if (!forecastHorizons.includes(value.forecast.horizon) || readiness.horizon !== value.forecast.horizon || readiness.modelId !== value.forecast.modelId) throw new Error('stock_intelligence_forecast_binding_invalid')
    if (JSON.stringify(readiness) !== JSON.stringify(recomputed)) throw new Error('stock_intelligence_forecast_readiness_not_recomputed')
    if (readiness.status === 'decision_support_ready' && (!readiness.gates.every((gate) => gate.passed) || !readiness.probabilitiesVisible || !readiness.intervalsMayUseCoverageLabels)) throw new Error('stock_intelligence_forecast_readiness_invalid')
    if (readiness.status !== 'decision_support_ready' && (readiness.probabilitiesVisible || readiness.intervalsMayUseCoverageLabels)) throw new Error('stock_intelligence_forecast_probability_leak')
  }
}
