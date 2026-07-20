import type { HoldingRow, Market, MarketPulse, MarketPulseCoverage, MarketPulseCoverageEntry, MarketPulseCoverageObservation, SignalRow } from '../types/dashboard.ts'

type PulseMarket = Exclude<Market, 'All Markets'>
type PulseDatasetIds = Partial<Record<PulseMarket, string>>

type PulseReaderOptions = {
  baseUrl?: string
  expectedCatalogVersion?: string
  datasetIds?: PulseDatasetIds
  accessPolicyId?: string
  schemaMajor?: number
  holdings: HoldingRow[]
  signals: SignalRow[]
  fetchImpl?: typeof fetch
  now?: Date
}

type V1Config = {
  baseUrl: string
  expectedCatalogVersion: string
  datasetIds: PulseDatasetIds
  accessPolicyId: string
  schemaMajor: number
}

type RawRow = Record<string, unknown>
type RawMetadata = {
  state: string
  degraded: boolean
  freshness: RawRow
  quality: RawRow
  lineage: RawRow | null
  receiptId: string | null
  dataThrough: string | null
  observedAt: string | null
  reasons: string[]
  sourceProofComplete: boolean
}
type ValidQueryEnvelope = {
  data: RawRow[]
  metadata: RawMetadata
}
type CompleteRawMetadata = Omit<RawMetadata, 'lineage' | 'receiptId' | 'dataThrough' | 'observedAt' | 'sourceProofComplete'> & {
  lineage: RawRow
  receiptId: string
  dataThrough: string
  observedAt: string
  sourceProofComplete: true
}
type UsableQueryEnvelope = Omit<ValidQueryEnvelope, 'metadata'> & { metadata: CompleteRawMetadata }
type AuditedCoverageEntry = MarketPulseCoverageEntry & { reasons?: string[] }
type AuditedMarketPulseCoverage = Omit<MarketPulseCoverage, 'entries'> & { entries: AuditedCoverageEntry[] }
type AuditedCoverageObservation = Omit<MarketPulseCoverageObservation, 'entries'> & { entries: AuditedCoverageEntry[] }
type MarketPulseReadResult = { pulses: MarketPulse[]; coverage: AuditedMarketPulseCoverage; coverageHistory: AuditedCoverageObservation[] }
type CacheEntry = { expiresAt: number; value: MarketPulseReadResult }
type PulseRequest = { market: PulseMarket; symbol: string; datasetId: string }
type PulseOutcome = PulseRequest & {
  status: MarketPulseCoverageEntry['status']
  pulse?: MarketPulse
  reasons?: string[]
}

const CATALOG_PATH = '/v1/catalog'
const QUERY_PATH = '/v1/query'
const CACHE_TTL_MS = 15_000
const REQUEST_TIMEOUT_MS = 900
const MAX_POINTS = 24
const cache = new Map<string, CacheEntry>()
const coverageHistory = new Map<string, AuditedCoverageObservation[]>()
const MAX_COVERAGE_OBSERVATIONS = 12
const MARKET_ORDER: PulseMarket[] = ['A-share', 'CNFutures', 'Crypto']
const DATASET_ID_PATTERN = /^[a-z0-9][a-z0-9._-]*$/

export async function readSharedSignalsMarketPulses({
  baseUrl,
  expectedCatalogVersion,
  datasetIds,
  accessPolicyId,
  schemaMajor,
  holdings,
  signals,
  fetchImpl = fetch,
  now = new Date(),
}: PulseReaderOptions): Promise<MarketPulseReadResult> {
  const representatives = selectRepresentatives(holdings, signals)
  const config = normalizeConfig({ baseUrl, expectedCatalogVersion, datasetIds, accessPolicyId, schemaMajor })
  if (!config) return unavailableResult(representatives, now)

  const requests = MARKET_ORDER.flatMap((market): PulseRequest[] => {
    const symbol = representatives.get(market)
    const datasetId = config.datasetIds[market]
    return symbol && datasetId ? [{ market, symbol, datasetId }] : []
  })
  if (!requests.length) return unavailableResult(representatives, now)

  const key = cacheKey(config, representatives)
  const cached = cache.get(key)
  if (cached && cached.expiresAt > now.getTime()) {
    return {
      ...cached.value,
      coverage: { ...cached.value.coverage, cacheState: 'cached' },
      coverageHistory: readCoverageHistory(key),
    }
  }

  const startedAt = Date.now()
  const catalogReady = await validateCatalog(config, requests, fetchImpl)
  const settled: PulseOutcome[] = catalogReady
    ? await Promise.all(requests.map((request) => queryPulse(config, request, fetchImpl, now)))
    : requests.map((request) => ({ ...request, status: 'unavailable' as const, reasons: ['catalog_unavailable'] }))
  const statusByMarket = new Map(settled.map((item) => [item.market, item]))
  const entries: AuditedCoverageEntry[] = MARKET_ORDER.map((market) => {
    const symbol = representatives.get(market)
    if (!symbol) return { market, status: 'no_representative' }
    if (!config.datasetIds[market]) return { market, symbol, status: 'unavailable' }
    const outcome = statusByMarket.get(market)
    return {
      market,
      symbol,
      status: outcome?.status ?? 'unavailable',
      ...(outcome?.reasons?.length ? { reasons: [...outcome.reasons] } : {}),
    }
  })
  const coverage: MarketPulseCoverage = {
    cacheState: 'fresh',
    entries,
    fetchedAt: now.toISOString(),
    requestedCount: representatives.size,
    sourcedCount: entries.filter((entry) => entry.status === 'sourced').length,
    sourceLatencyMs: Date.now() - startedAt,
  }
  appendCoverageObservation(key, coverage)
  const value: MarketPulseReadResult = {
    pulses: settled.flatMap((item) => item.pulse ? [item.pulse] : []),
    coverage,
    coverageHistory: readCoverageHistory(key),
  }
  if (catalogReady && settled.every((item) => item.status === 'sourced')) {
    cache.set(key, { expiresAt: now.getTime() + CACHE_TTL_MS, value })
  }
  return value
}

export function resetMarketPulseCacheForTests() {
  cache.clear()
  coverageHistory.clear()
}

function normalizeConfig({
  baseUrl,
  expectedCatalogVersion,
  datasetIds,
  accessPolicyId,
  schemaMajor,
}: Pick<PulseReaderOptions, 'baseUrl' | 'expectedCatalogVersion' | 'datasetIds' | 'accessPolicyId' | 'schemaMajor'>): V1Config | undefined {
  const normalizedBase = baseUrl?.trim().replace(/\/$/, '')
  const catalogVersion = exactNonEmptyText(expectedCatalogVersion)
  const policyId = exactNonEmptyText(accessPolicyId)
  if (!normalizedBase || !catalogVersion || !policyId || !datasetIds || !Number.isInteger(schemaMajor) || Number(schemaMajor) <= 0) return undefined
  try {
    const url = new URL(normalizedBase)
    if (!['http:', 'https:'].includes(url.protocol) || url.search || url.hash) return undefined
  } catch {
    return undefined
  }
  const normalizedDatasetIds: PulseDatasetIds = {}
  for (const market of MARKET_ORDER) {
    const datasetId = exactNonEmptyText(datasetIds[market])
    if (datasetId && DATASET_ID_PATTERN.test(datasetId)) normalizedDatasetIds[market] = datasetId
  }
  if (!Object.keys(normalizedDatasetIds).length) return undefined
  return {
    baseUrl: normalizedBase,
    expectedCatalogVersion: catalogVersion,
    datasetIds: normalizedDatasetIds,
    accessPolicyId: policyId,
    schemaMajor: Number(schemaMajor),
  }
}

function cacheKey(config: V1Config, representatives: Map<PulseMarket, string>) {
  const mappings = MARKET_ORDER.map((market) => `${market}:${config.datasetIds[market] ?? '-'}:${representatives.get(market) ?? '-'}`).join('|')
  return `${config.baseUrl}|${config.expectedCatalogVersion}|${config.accessPolicyId}|${config.schemaMajor}|${mappings}`
}

async function validateCatalog(config: V1Config, requests: PulseRequest[], fetchImpl: typeof fetch): Promise<boolean> {
  try {
    const response = await fetchImpl(`${config.baseUrl}${CATALOG_PATH}`, {
      method: 'GET',
      headers: {
        accept: 'application/json',
      },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
    if (!response.ok) return false
    const payload = await response.json()
    if (!isRecord(payload)
      || payload.api_version !== 'v1'
      || exactNonEmptyText(payload.catalog_version) !== config.expectedCatalogVersion
      || !exactNonEmptyText(payload.request_id)
      || !Array.isArray(payload.data)
    ) return false
    const catalogDatasetIds = new Set<string>()
    for (const row of payload.data) {
      if (!isRecord(row)) return false
      const datasetId = exactNonEmptyText(row.dataset_id)
      if (!datasetId || !DATASET_ID_PATTERN.test(datasetId) || catalogDatasetIds.has(datasetId)) return false
      catalogDatasetIds.add(datasetId)
    }
    return requests.every((request) => catalogDatasetIds.has(request.datasetId))
  } catch {
    return false
  }
}

async function queryPulse(
  config: V1Config,
  request: PulseRequest,
  fetchImpl: typeof fetch,
  now: Date,
): Promise<PulseOutcome> {
  try {
    const asOf = now.toISOString()
    const response = await fetchImpl(`${config.baseUrl}${QUERY_PATH}`, {
      method: 'POST',
      headers: {
        accept: 'application/json',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        dataset_id: request.datasetId,
        schema_major: config.schemaMajor,
        fields: fieldsForMarket(request.market),
        filters: filtersForMarket(request.market, request.symbol),
        as_of: asOf,
        limit: MAX_POINTS,
        cursor: null,
      }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
    if (!response.ok) return { ...request, status: 'unavailable' as const, reasons: [`query_http_${response.status}`] }
    const envelope = parseQueryEnvelope(await response.json(), config, request, asOf)
    if (!envelope) return { ...request, status: 'unavailable' as const, reasons: ['query_envelope_invalid'] }
    const metadata = envelope.metadata
    if (!metadataIsUsable(metadata)) {
      return {
        ...request,
        status: 'degraded' as const,
        reasons: deduplicate([
          ...envelope.metadata.reasons,
          ...(!envelope.metadata.sourceProofComplete ? ['dataset_source_proof_incomplete'] : []),
        ]),
      }
    }
    const pulse = normalizePulse(
      request.market,
      request.symbol,
      request.datasetId,
      { ...envelope, metadata },
      now,
    )
    if (!pulse) return { ...request, status: 'unavailable' as const, reasons: ['query_rows_unusable'] }
    if (pulse.freshness !== 'live') return { ...request, status: 'degraded' as const, reasons: ['row_freshness_not_live'] }
    return { ...request, status: 'sourced' as const, pulse }
  } catch {
    return { ...request, status: 'unavailable' as const, reasons: ['query_transport_unavailable'] }
  }
}

function parseQueryEnvelope(
  value: unknown,
  config: V1Config,
  request: PulseRequest,
  asOf: string,
): ValidQueryEnvelope | undefined {
  if (!isRecord(value)
    || value.api_version !== 'v1'
    || exactNonEmptyText(value.catalog_version) !== config.expectedCatalogVersion
    || !exactNonEmptyText(value.request_id)
    || value.dataset_id !== request.datasetId
    || !Array.isArray(value.data)
    || value.next_cursor !== null
    || !isRecord(value.metadata)
  ) return undefined
  const rows: RawRow[] = []
  for (const row of value.data) {
    if (!isRecord(row)) return undefined
    rows.push(row)
  }
  const rawMetadata = value.metadata
  const state = exactNonEmptyText(rawMetadata.state)
  for (const requiredField of ['lineage', 'receipt_id', 'data_through', 'observed_at']) {
    if (!(requiredField in rawMetadata)) return undefined
  }
  const rawLineage = rawMetadata.lineage
  const lineage = rawLineage === null ? null : isNonEmptyRecord(rawLineage) ? rawLineage : undefined
  const rawReceiptId = rawMetadata.receipt_id
  const receiptId = rawReceiptId === null ? null : exactNonEmptyText(rawReceiptId)
  const rawDataThrough = rawMetadata.data_through
  const dataThrough = rawDataThrough === null ? null : awareIsoText(rawDataThrough)
  const rawObservedAt = rawMetadata.observed_at
  const observedAt = rawObservedAt === null ? null : awareIsoText(rawObservedAt)
  const reasons = stringList(rawMetadata.reasons)
  if (!state
    || typeof rawMetadata.degraded !== 'boolean'
    || !isNonEmptyRecord(rawMetadata.freshness)
    || !isNonEmptyRecord(rawMetadata.quality)
    || lineage === undefined
    || receiptId === undefined
    || dataThrough === undefined
    || observedAt === undefined
    || !reasons
  ) return undefined
  const readyState = ['ready', 'healthy', 'ok', 'available'].includes(state.toLowerCase())
  const sourceProofComplete = Boolean(lineage && receiptId && dataThrough && observedAt)
  if (!rawMetadata.degraded && readyState && !sourceProofComplete) return undefined
  if (dataThrough && observedAt && Date.parse(dataThrough) > Date.parse(observedAt)) return undefined
  if (dataThrough && Date.parse(dataThrough) > Date.parse(asOf)) return undefined
  return {
    data: rows,
    metadata: {
      state,
      degraded: rawMetadata.degraded,
      freshness: rawMetadata.freshness,
      quality: rawMetadata.quality,
      lineage,
      receiptId,
      dataThrough,
      observedAt,
      reasons,
      sourceProofComplete,
    },
  }
}

function metadataIsUsable(metadata: RawMetadata): metadata is CompleteRawMetadata {
  if (
    !metadata.sourceProofComplete
    || !metadata.lineage
    || !metadata.receiptId
    || !metadata.dataThrough
    || !metadata.observedAt
  ) return false
  if (metadata.degraded || !['ready', 'healthy', 'ok', 'available'].includes(metadata.state.toLowerCase())) return false
  const freshnessState = nestedState(metadata.freshness)
  const qualityState = nestedState(metadata.quality)
  const lineageState = nestedState(metadata.lineage)
  if (!freshnessState || !['fresh', 'ready', 'healthy', 'ok', 'available'].includes(freshnessState)) return false
  if (!qualityState || !['valid', 'ready', 'healthy', 'ok', 'available'].includes(qualityState)) return false
  if (!lineageState || !['complete', 'ready', 'healthy', 'ok', 'available'].includes(lineageState)) return false
  if (metadata.freshness.fresh !== true || metadata.freshness.stale !== false) return false
  if (metadata.quality.valid !== true) return false
  if (metadata.lineage.complete !== true || metadata.lineage.provider_neutral !== true) return false
  return true
}

function fieldsForMarket(market: PulseMarket) {
  if (market === 'Crypto') return ['symbol', 'close', 'price', 'high', 'low', 'volume', 'bar_time']
  return ['symbol', 'close', 'high', 'low', 'volume', 'bar_time']
}

function filtersForMarket(_market: PulseMarket, symbol: string) {
  return { symbol }
}

function selectRepresentatives(holdings: HoldingRow[], signals: SignalRow[]) {
  const selected = new Map<PulseMarket, string>()
  for (const item of [...holdings, ...signals]) {
    if (item.market === 'All Markets' || selected.has(item.market)) continue
    const symbol = item.market === 'A-share' ? (item.marketDataSymbol ?? item.symbol).trim() : item.marketDataSymbol?.trim()
    if (symbol) selected.set(item.market, symbol)
  }
  return selected
}

function unavailableResult(representatives: Map<PulseMarket, string>, now: Date): MarketPulseReadResult {
  return {
    pulses: [],
    coverage: {
      cacheState: 'fresh',
      entries: MARKET_ORDER.map((market) => {
        const symbol = representatives.get(market)
        return symbol ? { market, symbol, status: 'unavailable' } : { market, status: 'no_representative' }
      }),
      fetchedAt: now.toISOString(),
      requestedCount: representatives.size,
      sourcedCount: 0,
      sourceLatencyMs: 0,
    },
    coverageHistory: [],
  }
}

function appendCoverageObservation(key: string, coverage: AuditedMarketPulseCoverage) {
  const observation: AuditedCoverageObservation = {
    entries: coverage.entries.map((entry) => ({ ...entry, ...(entry.reasons ? { reasons: [...entry.reasons] } : {}) })),
    fetchedAt: coverage.fetchedAt,
    requestedCount: coverage.requestedCount,
    sourcedCount: coverage.sourcedCount,
    sourceLatencyMs: coverage.sourceLatencyMs,
  }
  const next = [...(coverageHistory.get(key) ?? []), observation].slice(-MAX_COVERAGE_OBSERVATIONS)
  coverageHistory.set(key, next)
}

function readCoverageHistory(key: string) {
  return (coverageHistory.get(key) ?? []).map((observation) => ({
    ...observation,
    entries: observation.entries.map((entry) => ({ ...entry, ...(entry.reasons ? { reasons: [...entry.reasons] } : {}) })),
  }))
}

function normalizePulse(
  market: PulseMarket,
  symbol: string,
  datasetId: string,
  envelope: UsableQueryEnvelope,
  now: Date,
): MarketPulse | null {
  const rows = envelope.data
    .filter((row) => rowMatchesSymbol(row, symbol))
  const timestampCeiling = Math.min(now.getTime(), Date.parse(envelope.metadata.dataThrough))
  const samples = rows
    .map((row, index) => ({ row, index, price: readPrice(row), time: rowTimestamp(row, market) }))
    .filter((item): item is typeof item & { price: number; time: number } => (
      item.price !== undefined
      && item.time !== undefined
      && item.time <= timestampCeiling
    ))
    .sort((left, right) => left.time - right.time || left.index - right.index)
    .slice(-MAX_POINTS)
  if (!samples.length) return null
  const sampleRows = samples.map((item) => item.row)
  const points = samples.map((item) => item.price)
  const latest = samples[samples.length - 1].row
  const previous = points.length > 1 ? points[points.length - 2] : undefined
  const lastPrice = points[points.length - 1]
  const updatedAt = readString(latest, ['bar_time', 'trade_time', 'price_time', 'updated_at', 'collected_at', 'open_time', 'trade_date'])
  const highValues = sampleRows.map((row) => readNumber(row, ['high'])).filter((value): value is number => value !== undefined)
  const lowValues = sampleRows.map((row) => readNumber(row, ['low'])).filter((value): value is number => value !== undefined)
  const volumes = sampleRows.map((row) => readNumber(row, ['volume', 'vol'])).filter((value): value is number => value !== undefined)
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
    source: `TradingDatas V1:${datasetId}:${envelope.metadata.receiptId}`,
  }
}

function rowMatchesSymbol(row: RawRow, expected: string) {
  const actual = readString(row, ['symbol', 'ts_code'])
  return actual === expected
}

function freshness(updatedAt: string | undefined, market: PulseMarket, now: Date): MarketPulse['freshness'] {
  if (!updatedAt) return 'degraded'
  const time = parseTimestamp(updatedAt, market)
  if (time === undefined) return 'degraded'
  const maxAge = market === 'A-share' || market === 'CNFutures' ? 15 * 60_000 : 45 * 60_000
  return now.getTime() - time <= maxAge ? 'live' : 'stale'
}

function readPrice(row: RawRow) { return readNumber(row, ['close', 'price', 'latest_price', 'last_price']) }
function rowTimestamp(row: RawRow, market: PulseMarket) {
  const value = readString(row, ['bar_time', 'trade_time', 'price_time', 'updated_at', 'collected_at', 'open_time', 'trade_date'])
  return value ? parseTimestamp(value, market) : undefined
}
function parseTimestamp(value: string, market: PulseMarket) {
  const compact = value.match(/^(\d{4})(\d{2})(\d{2})$/)
  if (compact) return Date.UTC(Number(compact[1]), Number(compact[2]) - 1, Number(compact[3]))
  const cnLocal = value.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  const normalized = cnLocal && (market === 'A-share' || market === 'CNFutures') ? `${value.replace(' ', 'T')}+08:00` : value
  const parsed = new Date(normalized).getTime()
  return Number.isFinite(parsed) ? parsed : undefined
}
function readNumber(row: RawRow, keys: string[]) { for (const key of keys) { const value = Number(row[key]); if (Number.isFinite(value)) return value } return undefined }
function readString(row: RawRow, keys: string[]) { for (const key of keys) { const value = row[key]; if (typeof value === 'string' && value.trim()) return value.trim() } return undefined }
function exactNonEmptyText(value: unknown) { return typeof value === 'string' && value.length > 0 && value === value.trim() ? value : undefined }
function awareIsoText(value: unknown) {
  const text = exactNonEmptyText(value)
  if (!text || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(text)) return undefined
  return Number.isFinite(Date.parse(text)) ? text : undefined
}
function stringList(value: unknown) {
  if (!Array.isArray(value)) return undefined
  const strings = value.map(exactNonEmptyText)
  return strings.every((item): item is string => Boolean(item)) ? strings : undefined
}
function deduplicate(values: string[]) { return [...new Set(values)] }
function isRecord(value: unknown): value is RawRow { return Boolean(value) && typeof value === 'object' && !Array.isArray(value) }
function isNonEmptyRecord(value: unknown): value is RawRow { return isRecord(value) && Object.keys(value).length > 0 }
function nestedState(value: RawRow) { return exactNonEmptyText(value.state)?.toLowerCase() }
function round(value: number) { return Math.round(value * 10_000) / 10_000 }
