import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { HoldingRow, Market, SignalRow } from '../types/dashboard'
import { readSharedSignalsMarketPulses, resetMarketPulseCacheForTests } from './sharedSignalsMarketPulse'

const DATASET_IDS: Partial<Record<Exclude<Market, 'All Markets'>, string>> = {
  'A-share': 'cn_equity_intraday_fixture',
  Crypto: 'crypto_intraday_fixture',
  CNFutures: 'cn_futures_intraday_fixture',
}
const V1_CONFIG = {
  baseUrl: 'https://tradingdatas.fixture.invalid',
  expectedCatalogVersion: 'catalog-fixture-v1',
  accessPolicyId: 'ta-front-readonly-fixture',
  schemaMajor: 1,
  datasetIds: DATASET_IDS,
}

const holding = (market: HoldingRow['market'], symbol: string): HoldingRow => ({
  market, symbol, name: symbol, weight: '0%', pnl: '—', risk: '正常', role: '观察',
})

const signal = (market: SignalRow['market'], symbol: string): SignalRow => ({
  market, symbol, name: symbol, method: 'test', status: 'pending', impact: '—', confidence: '—', age: '—', reason: '—', next: '—', steps: 1,
})

function catalogResponse(datasetIds = Object.values(DATASET_IDS)) {
  return new Response(JSON.stringify({
    api_version: 'v1',
    catalog_version: V1_CONFIG.expectedCatalogVersion,
    request_id: 'catalog-request-1',
    data: datasetIds.map((datasetId) => ({ dataset_id: datasetId })),
  }), { status: 200 })
}

function queryResponse(
  datasetId: string,
  data: Array<Record<string, unknown>>,
  metadataOverrides: Record<string, unknown> = {},
  envelopeOverrides: Record<string, unknown> = {},
) {
  return new Response(JSON.stringify({
    api_version: 'v1',
    catalog_version: V1_CONFIG.expectedCatalogVersion,
    request_id: `query-${datasetId}`,
    dataset_id: datasetId,
    data,
    next_cursor: null,
    metadata: {
      state: 'ready',
      degraded: false,
      freshness: { state: 'fresh', fresh: true, stale: false },
      quality: { state: 'valid', valid: true },
      lineage: { state: 'complete', complete: true, provider_neutral: true },
      receipt_id: `receipt-${datasetId}`,
      data_through: '2026-07-11T01:35:00+00:00',
      observed_at: '2026-07-11T01:36:00+00:00',
      reasons: [],
      ...metadataOverrides,
    },
    ...envelopeOverrides,
  }), { status: 200 })
}

function v1Fetch(
  queryHandler: (body: Record<string, unknown>, init?: RequestInit) => Response | Promise<Response>,
) {
  return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/v1/catalog')) return catalogResponse()
    expect(url).toBe(`${V1_CONFIG.baseUrl}/v1/query`)
    return queryHandler(JSON.parse(String(init?.body)) as Record<string, unknown>, init)
  })
}

describe('TradingDatas market pulse reader (compatibility module)', () => {
  beforeEach(() => resetMarketPulseCacheForTests())

  it('uses only the configured V1 catalog/query contract and sends explicit schema_major without order', async () => {
    const fetchImpl = v1Fetch((body, init) => {
      expect(init?.method).toBe('POST')
      expect(init?.headers).toMatchObject({
        accept: 'application/json',
        'content-type': 'application/json',
      })
      expect(init?.headers).not.toHaveProperty('x-access-policy')
      expect(body).toEqual({
        dataset_id: DATASET_IDS['A-share'],
        schema_major: V1_CONFIG.schemaMajor,
        fields: ['symbol', 'close', 'high', 'low', 'volume', 'bar_time'],
        filters: { symbol: '600519.SH' },
        as_of: '2026-07-11T01:40:00.000Z',
        limit: 24,
        cursor: null,
      })
      return queryResponse(String(body.dataset_id), [
        { symbol: '600519.SH', close: 1410, high: 1420, low: 1400, volume: 800, bar_time: '2026-07-11T09:30:00+08:00' },
        { symbol: '600519.SH', close: 1424.1, high: 1430, low: 1410, volume: 1200, bar_time: '2026-07-11T09:35:00+08:00' },
      ])
    })

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      now: new Date('2026-07-11T09:40:00+08:00'),
      signals: [],
    })

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(String(fetchImpl.mock.calls[0][0])).toBe(`${V1_CONFIG.baseUrl}/v1/catalog`)
    expect(result.pulses[0]).toMatchObject({
      market: 'A-share',
      symbol: '600519.SH',
      lastPrice: 1424.1,
      changePct: 1,
      high: 1430,
      low: 1400,
      volume: 2000,
      freshness: 'live',
      source: `TradingDatas V1:${DATASET_IDS['A-share']}:receipt-${DATASET_IDS['A-share']}`,
    })
    expect(result.pulses[0].points).toEqual([1410, 1424.1])
  })

  it('fails closed without fetching when the V1 base, catalog, policy, or market dataset mapping is incomplete', async () => {
    const fetchImpl = vi.fn()

    const result = await readSharedSignalsMarketPulses({
      baseUrl: V1_CONFIG.baseUrl,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(fetchImpl).not.toHaveBeenCalled()
    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({
      market: 'A-share', symbol: '600519.SH', status: 'unavailable',
    }))
  })

  it('does not treat HTTP 200 as usable when V1 dataset metadata is degraded or stale', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(String(body.dataset_id), [
      { symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' },
    ], {
      state: 'stale',
      degraded: true,
      freshness: { state: 'stale', fresh: false, stale: true },
      reasons: ['dataset_stale'],
    }))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({
      market: 'A-share', symbol: '600519.SH', status: 'degraded', reasons: ['dataset_stale'],
    }))
  })

  it('preserves null-proof impairment reasons, fails closed, and never caches the result', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(String(body.dataset_id), [], {
      state: 'unobserved',
      degraded: true,
      freshness: { state: 'unobserved', fresh: false, stale: false },
      quality: { state: 'unobserved', valid: false },
      lineage: null,
      receipt_id: null,
      data_through: null,
      observed_at: null,
      reasons: ['provider_not_observed'],
    }))
    const input = {
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    }

    const first = await readSharedSignalsMarketPulses(input)
    const second = await readSharedSignalsMarketPulses(input)

    expect(first.pulses).toEqual([])
    expect(first.coverage.entries).toContainEqual(expect.objectContaining({
      market: 'A-share',
      symbol: '600519.SH',
      status: 'degraded',
      reasons: ['provider_not_observed', 'dataset_source_proof_incomplete'],
    }))
    expect(second.coverage.cacheState).toBe('fresh')
    expect(fetchImpl).toHaveBeenCalledTimes(4)
  })

  it.each([
    ['missing freshness state', { freshness: { fresh: true, stale: false } }],
    ['unknown quality state', { quality: { state: 'excellent', valid: true } }],
    ['invalid quality flag', { quality: { state: 'valid', valid: false } }],
    ['incomplete lineage', { lineage: { state: 'complete', complete: false, provider_neutral: true } }],
    ['provider-specific lineage', { lineage: { state: 'complete', complete: true, provider_neutral: false } }],
  ])('fails closed for %s instead of treating non-empty metadata as evidence', async (_label, metadata) => {
    const fetchImpl = v1Fetch((body) => queryResponse(String(body.dataset_id), [
      { symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' },
    ], metadata))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      now: new Date('2026-07-11T09:40:00+08:00'),
      signals: [],
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({
      market: 'A-share', symbol: '600519.SH', status: 'degraded',
    }))
  })

  it('rejects a query envelope whose catalog or dataset identity does not match explicit config', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(
      String(body.dataset_id),
      [{ symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' }],
      {},
      { dataset_id: 'forged_dataset' },
    ))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({ market: 'A-share', status: 'unavailable' }))
  })

  it('fails closed when the bounded pulse query indicates an unread next page', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(
      String(body.dataset_id),
      [{ symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' }],
      {},
      { next_cursor: 'unread-next-page' },
    ))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({ market: 'A-share', status: 'unavailable' }))
  })

  it('rejects a row whose entity identity is absent instead of trusting only the request filter', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(
      String(body.dataset_id),
      [{ close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' }],
    ))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({ market: 'A-share', status: 'unavailable' }))
  })

  it('rejects future-dated rows even when HTTP and envelope metadata otherwise look healthy', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(
      String(body.dataset_id),
      [{ symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:45:00+08:00' }],
    ))

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toEqual([])
    expect(result.coverage.entries).toContainEqual(expect.objectContaining({ market: 'A-share', status: 'unavailable' }))
  })

  it('does not cache transport failures or invent rows', async () => {
    const fetchImpl = vi.fn(async () => { throw new Error('unavailable') })
    const input = { ...V1_CONFIG, fetchImpl, holdings: [holding('A-share', '000001.SZ')], signals: [] }

    const first = await readSharedSignalsMarketPulses(input)
    const second = await readSharedSignalsMarketPulses(input)
    expect(first.pulses).toEqual([])
    expect(first.coverage.cacheState).toBe('fresh')
    expect(second.pulses).toEqual([])
    expect(second.coverage.cacheState).toBe('fresh')
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('orders newest-first V1 rows chronologically before deriving the pulse', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(String(body.dataset_id), [
      { symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' },
      { symbol: '600519.SH', close: 1410, bar_time: '2026-07-11T09:30:00+08:00' },
    ]))

    const { pulses: [pulse] } = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(pulse.points).toEqual([1410, 1424.1])
    expect(pulse.lastPrice).toBe(1424.1)
  })

  it('treats compact trade dates as valid freshness evidence for an active market', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(
      String(body.dataset_id),
      [{ symbol: 'IF2601.CFFEX', close: 4012.4, trade_date: '20260711' }],
      { data_through: '2026-07-11T00:05:00+00:00', observed_at: '2026-07-11T00:06:00+00:00' },
    ))

    const { pulses: [pulse] } = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [{ ...holding('CNFutures', 'IF2601.CFFEX'), marketDataSymbol: 'IF2601.CFFEX' }],
      signals: [],
      now: new Date('2026-07-11T08:05:00+08:00'),
    })

    expect(pulse.updatedAt).toBe('20260711')
    expect(pulse.freshness).toBe('live')
  })

  it('reports sourced, unavailable, degraded and unmapped coverage without guessing identifiers', async () => {
    const fetchImpl = v1Fetch((body) => {
      if (body.dataset_id === DATASET_IDS.Crypto) throw new Error('upstream unavailable')
      if (body.dataset_id === DATASET_IDS.CNFutures) return queryResponse(String(body.dataset_id), [], { degraded: true, state: 'failed', reasons: ['upstream_failed'] })
      return queryResponse(String(body.dataset_id), [{ symbol: '600519.SH', close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' }])
    })

    const result = await readSharedSignalsMarketPulses({
      ...V1_CONFIG,
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [{ ...signal('Crypto', 'BTCUSDT'), marketDataSymbol: 'BTCUSDT' }, { ...signal('CNFutures', 'IF2601.CFFEX'), marketDataSymbol: 'IF2601.CFFEX' }],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(result.pulses).toHaveLength(1)
    expect(result.coverage.entries).toEqual(expect.arrayContaining([
      expect.objectContaining({ market: 'A-share', symbol: '600519.SH', status: 'sourced' }),
      expect.objectContaining({ market: 'Crypto', symbol: 'BTCUSDT', status: 'unavailable' }),
      expect.objectContaining({ market: 'CNFutures', symbol: 'IF2601.CFFEX', status: 'degraded' }),
    ]))
    expect(result.coverage).toMatchObject({ cacheState: 'fresh', sourcedCount: 1, requestedCount: 3 })
  })

  it('uses only explicit non-A-share symbols and retains fresh-only coverage history', async () => {
    const fetchImpl = v1Fetch((body) => queryResponse(String(body.dataset_id), [
      { symbol: 'BTCUSDT', close: 62_000, bar_time: '2026-07-11T09:35:00+08:00' },
    ]))
    const explicitCrypto = { ...signal('Crypto', 'BTC-USDT'), marketDataSymbol: 'BTCUSDT' }
    const unmappedCrypto = signal('Crypto', 'ETH-USDT')

    const unmapped = await readSharedSignalsMarketPulses({ ...V1_CONFIG, fetchImpl, holdings: [], signals: [unmappedCrypto], now: new Date('2026-07-11T09:39:00+08:00') })
    const first = await readSharedSignalsMarketPulses({ ...V1_CONFIG, fetchImpl, holdings: [], signals: [explicitCrypto], now: new Date('2026-07-11T09:40:00+08:00') })
    const cached = await readSharedSignalsMarketPulses({ ...V1_CONFIG, fetchImpl, holdings: [], signals: [explicitCrypto], now: new Date('2026-07-11T09:40:10+08:00') })
    const refreshed = await readSharedSignalsMarketPulses({ ...V1_CONFIG, fetchImpl, holdings: [], signals: [explicitCrypto], now: new Date('2026-07-11T09:40:20+08:00') })

    expect(unmapped.coverage.entries).toContainEqual(expect.objectContaining({ market: 'Crypto', status: 'no_representative' }))
    expect(first.coverage.entries).toContainEqual(expect.objectContaining({ market: 'Crypto', symbol: 'BTCUSDT', status: 'sourced' }))
    expect(first.coverageHistory).toHaveLength(1)
    expect(cached.coverageHistory).toHaveLength(1)
    expect(refreshed.coverageHistory).toHaveLength(2)
    const queryBodies = fetchImpl.mock.calls
      .filter(([input]) => String(input).endsWith('/v1/query'))
      .map(([, init]) => JSON.parse(String(init?.body)))
    expect(queryBodies).toHaveLength(2)
    expect(queryBodies[0]).toMatchObject({ filters: { symbol: 'BTCUSDT' } })
  })
})
