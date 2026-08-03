import { isAshareSymbol } from './types.ts'

export const TRADING_COPILOT_TRACKING_UNIVERSE_ROUTE = '/api/trading-copilot/tracking-universe'

export type TrackingUniverseItem = { symbol: string; name: string }
export type TrackingUniverse = {
  contractId: 'tradingagent.trading_copilot_tracking_universe.v1'
  generatedAt: string
  items: TrackingUniverseItem[]
}

export async function loadTrackingUniverse(fetcher: typeof fetch = fetch): Promise<TrackingUniverse | null> {
  try {
    const response = await fetcher(TRADING_COPILOT_TRACKING_UNIVERSE_ROUTE, { headers: { Accept: 'application/json' } })
    if (!response.ok) return null
    const payload = await response.json() as unknown
    assertTrackingUniverse(payload)
    return payload
  } catch {
    return null
  }
}

export function assertTrackingUniverse(payload: unknown): asserts payload is TrackingUniverse {
  if (!payload || typeof payload !== 'object') throw new Error('tracking_universe_invalid')
  const value = payload as Partial<TrackingUniverse>
  if (!hasOnlyKeys(value, ['contractId', 'generatedAt', 'items']) || value.contractId !== 'tradingagent.trading_copilot_tracking_universe.v1' || !isTimestamp(value.generatedAt) || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > 500) {
    throw new Error('tracking_universe_invalid')
  }
  const symbols = new Set<string>()
  for (const item of value.items) {
    if (!item || typeof item !== 'object' || !hasOnlyKeys(item, ['symbol', 'name']) || !isAshareSymbol(item.symbol) || !item.name?.trim() || symbols.has(item.symbol)) throw new Error('tracking_universe_item_invalid')
    symbols.add(item.symbol)
  }
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

function hasOnlyKeys(value: object, allowed: string[]) {
  return Object.keys(value).every((key) => allowed.includes(key))
}
