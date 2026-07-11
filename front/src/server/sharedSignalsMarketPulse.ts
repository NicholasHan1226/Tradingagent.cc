import type { HoldingRow, Market, MarketPulse, SignalRow } from '../types/dashboard.ts'

type PulseReaderOptions = {
  baseUrl?: string
  holdings: HoldingRow[]
  signals: SignalRow[]
  fetchImpl?: typeof fetch
  now?: Date
}

type ApiPayload = { data?: unknown[]; metadata?: { degraded?: boolean }; source?: string }
type RawRow = Record<string, unknown>
type CacheEntry = { expiresAt: number; value: MarketPulse[] }

const CACHE_TTL_MS = 15_000
const REQUEST_TIMEOUT_MS = 900
const MAX_POINTS = 24
const cache = new Map<string, CacheEntry>()
const MARKET_ORDER: Array<Exclude<Market, 'All Markets'>> = ['A-share', 'US', 'Crypto', 'HK', 'PM', 'CNFutures']

export async function readSharedSignalsMarketPulses({ baseUrl, holdings, signals, fetchImpl = fetch, now = new Date() }: PulseReaderOptions): Promise<MarketPulse[]> {
  const normalizedBase = baseUrl?.trim().replace(/\/$/, '')
  if (!normalizedBase) return []
  const representatives = selectRepresentatives(holdings, signals)
  const requests = MARKET_ORDER.flatMap((market) => {
    const symbol = representatives.get(market)
    return symbol ? [{ market, symbol, url: pulseUrl(normalizedBase, market, symbol) }] : []
  })
  const key = `${normalizedBase}|${requests.map((item) => `${item.market}:${item.symbol}`).join('|')}`
  const cached = cache.get(key)
  if (cached && cached.expiresAt > now.getTime()) return cached.value

  const settled = await Promise.all(requests.map(async (request) => {
    try {
      const response = await fetchImpl(request.url, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
      if (!response.ok) return null
      const payload = await response.json() as ApiPayload
      return normalizePulse(request.market, request.symbol, payload, now)
    } catch {
      return null
    }
  }))
  const value = settled.filter((item): item is MarketPulse => item !== null)
  cache.set(key, { expiresAt: now.getTime() + CACHE_TTL_MS, value })
  return value
}

export function resetMarketPulseCacheForTests() {
  cache.clear()
}

function selectRepresentatives(holdings: HoldingRow[], signals: SignalRow[]) {
  const selected = new Map<Exclude<Market, 'All Markets'>, string>()
  for (const item of [...holdings, ...signals]) {
    if (item.market === 'All Markets' || selected.has(item.market)) continue
    const symbol = item.symbol.trim()
    if (symbol) selected.set(item.market, symbol)
  }
  return selected
}

function pulseUrl(baseUrl: string, market: Exclude<Market, 'All Markets'>, symbol: string) {
  const encoded = encodeURIComponent(symbol)
  if (market === 'A-share') return `${baseUrl}/realtime_5min?market=Ashare&ts_code=${encoded}&limit=${MAX_POINTS}`
  if (market === 'CNFutures') return `${baseUrl}/realtime_5min?market=Futures&ts_code=${encoded}&limit=${MAX_POINTS}`
  if (market === 'Crypto') return `${baseUrl}/crypto?symbol=${encoded}&limit=${MAX_POINTS}`
  if (market === 'PM') return `${baseUrl}/pm_prices?market_id=${encoded}&limit=${MAX_POINTS}`
  return `${baseUrl}/market_data?ts_code=${encoded}&freq=daily&limit=${MAX_POINTS}`
}

function normalizePulse(market: Exclude<Market, 'All Markets'>, symbol: string, payload: ApiPayload, now: Date): MarketPulse | null {
  if (payload.metadata?.degraded) return null
  const rows = (payload.data ?? []).filter(isRecord).filter((row) => market !== 'PM' || readOutcome(row) === 'yes')
  const samples = rows
    .map((row, index) => ({ row, index, price: readPrice(row), time: rowTimestamp(row, market) }))
    .filter((item): item is typeof item & { price: number } => item.price !== undefined)
    .sort((left, right) => left.time !== undefined && right.time !== undefined ? left.time - right.time : left.index - right.index)
    .slice(-MAX_POINTS)
  if (!samples.length) return null
  const points = samples.map((item) => item.price)
  const latest = samples[samples.length - 1].row
  const previous = points.length > 1 ? points[points.length - 2] : undefined
  const lastPrice = points[points.length - 1]
  const updatedAt = readString(latest, ['bar_time', 'trade_time', 'price_time', 'updated_at', 'collected_at', 'open_time', 'trade_date'])
  const highValues = rows.map((row) => readNumber(row, ['high'])).filter((value): value is number => value !== undefined)
  const lowValues = rows.map((row) => readNumber(row, ['low'])).filter((value): value is number => value !== undefined)
  const volumes = rows.map((row) => readNumber(row, ['volume', 'vol'])).filter((value): value is number => value !== undefined)
  return {
    market,
    symbol,
    lastPrice,
    changePct: previous && previous !== 0 ? round(((lastPrice - previous) / previous) * 100) : undefined,
    high: highValues.length ? Math.max(...highValues) : undefined,
    low: lowValues.length ? Math.min(...lowValues) : undefined,
    volume: volumes.length ? round(volumes.reduce((sum, value) => sum + value, 0)) : undefined,
    updatedAt,
    freshness: freshness(updatedAt, market, now),
    points,
    source: payload.source ?? 'SharedSignals',
  }
}

function freshness(updatedAt: string | undefined, market: Exclude<Market, 'All Markets'>, now: Date): MarketPulse['freshness'] {
  if (!updatedAt) return 'degraded'
  const time = parseTimestamp(updatedAt, market)
  if (time === undefined) return 'degraded'
  const maxAge = market === 'A-share' || market === 'CNFutures' ? 15 * 60_000 : market === 'Crypto' || market === 'PM' ? 45 * 60_000 : 36 * 60 * 60_000
  return now.getTime() - time <= maxAge ? 'live' : 'stale'
}

function readPrice(row: RawRow) { return readNumber(row, ['close', 'price', 'latest_price', 'last_price']) }
function rowTimestamp(row: RawRow, market: Exclude<Market, 'All Markets'>) {
  const value = readString(row, ['bar_time', 'trade_time', 'price_time', 'updated_at', 'collected_at', 'open_time', 'trade_date'])
  return value ? parseTimestamp(value, market) : undefined
}
function parseTimestamp(value: string, market: Exclude<Market, 'All Markets'>) {
  const compact = value.match(/^(\d{4})(\d{2})(\d{2})$/)
  if (compact) return Date.UTC(Number(compact[1]), Number(compact[2]) - 1, Number(compact[3]))
  const cnLocal = value.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  const normalized = cnLocal && (market === 'A-share' || market === 'CNFutures') ? `${value.replace(' ', 'T')}+08:00` : value
  const parsed = new Date(normalized).getTime()
  return Number.isFinite(parsed) ? parsed : undefined
}
function readOutcome(row: RawRow) {
  const direct = readString(row, ['outcome'])
  if (direct) return direct.toLowerCase()
  if (typeof row.raw_json !== 'string') return undefined
  try {
    const raw = JSON.parse(row.raw_json) as RawRow
    return readString(raw, ['outcome'])?.toLowerCase()
  } catch {
    return undefined
  }
}
function readNumber(row: RawRow, keys: string[]) { for (const key of keys) { const value = Number(row[key]); if (Number.isFinite(value)) return value } return undefined }
function readString(row: RawRow, keys: string[]) { for (const key of keys) { const value = row[key]; if (typeof value === 'string' && value.trim()) return value.trim() } return undefined }
function isRecord(value: unknown): value is RawRow { return Boolean(value) && typeof value === 'object' && !Array.isArray(value) }
function round(value: number) { return Math.round(value * 10_000) / 10_000 }
