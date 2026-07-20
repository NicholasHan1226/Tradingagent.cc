import { mkdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetMarketPulseCacheForTests } from './sharedSignalsMarketPulse'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot'

const DATASET_ID = 'fixture.crypto.market_pulse'
const BASE_URL = 'https://tradingdatas.fixture.invalid'
const CATALOG_VERSION = 'catalog-fixture-v1'

async function createWorkspaceWithCryptoRepresentative() {
  const root = join(tmpdir(), `ta-front-tradingdatas-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  await mkdir(join(root, 'TradingAgent/shared/accounting'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/shared/review/daily'), { recursive: true })
  await mkdir(join(root, 'signals/pending'), { recursive: true })
  await mkdir(join(root, 'signals/filled'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/signals/positions'), { recursive: true })
  await writeFile(join(root, 'signals/pending/BTCUSDT.json'), JSON.stringify({
    order_id: 'fixture-crypto-opportunity',
    ts_code: 'BTCUSDT',
    market: 'crypto',
    market_data_symbol: 'BTCUSDT',
    status: 'pending',
  }))
  return root
}

function configureTradingDatas(schemaMajor: string | undefined) {
  vi.stubEnv('TRADINGDATAS_API_URL', BASE_URL)
  vi.stubEnv('TRADINGDATAS_CATALOG_VERSION', CATALOG_VERSION)
  vi.stubEnv('TRADINGDATAS_SCHEMA_MAJOR', schemaMajor)
  vi.stubEnv('TRADINGDATAS_ACCESS_POLICY_ID', 'ta-front-readonly-fixture')
  vi.stubEnv('TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON', JSON.stringify({ Crypto: DATASET_ID }))
}

function catalogResponse() {
  return new Response(JSON.stringify({
    api_version: 'v1',
    catalog_version: CATALOG_VERSION,
    request_id: 'fixture-catalog-request',
    data: [{ dataset_id: DATASET_ID }],
  }), { status: 200 })
}

function queryResponse() {
  return new Response(JSON.stringify({
    api_version: 'v1',
    catalog_version: CATALOG_VERSION,
    request_id: 'fixture-query-request',
    dataset_id: DATASET_ID,
    data: [{ symbol: 'BTCUSDT', close: 66_000, bar_time: '2026-07-20T01:35:00+00:00' }],
    next_cursor: null,
    metadata: {
      state: 'ready',
      degraded: false,
      freshness: { state: 'fresh', fresh: true, stale: false },
      quality: { state: 'valid', valid: true },
      lineage: { state: 'complete', complete: true, provider_neutral: true },
      receipt_id: 'fixture-receipt-1',
      data_through: '2026-07-20T01:35:00+00:00',
      observed_at: '2026-07-20T01:36:00+00:00',
      reasons: [],
    },
  }), { status: 200 })
}

describe.sequential('TradingAgent snapshot TradingDatas configuration boundary', () => {
  beforeEach(() => resetMarketPulseCacheForTests())

  afterEach(() => {
    resetMarketPulseCacheForTests()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('passes a strictly parsed TRADINGDATAS_SCHEMA_MAJOR to the provider-neutral query', async () => {
    const root = await createWorkspaceWithCryptoRepresentative()
    configureTradingDatas('7')
    const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url === `${BASE_URL}/v1/catalog`) return catalogResponse()
      expect(url).toBe(`${BASE_URL}/v1/query`)
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>
      expect(body).toEqual({
        dataset_id: DATASET_ID,
        schema_major: 7,
        fields: ['symbol', 'close', 'price', 'high', 'low', 'volume', 'bar_time'],
        filters: { symbol: 'BTCUSDT' },
        as_of: '2026-07-20T01:40:00.000Z',
        limit: 24,
        cursor: null,
      })
      expect(body).not.toHaveProperty('order')
      return queryResponse()
    })
    vi.stubGlobal('fetch', fetchImpl)

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-20T01:40:00.000Z'),
    })

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(snapshot.marketPulses).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      symbol: 'BTCUSDT',
      source: `TradingDatas V1:${DATASET_ID}:fixture-receipt-1`,
    }))
  })

  it.each([undefined, '', '0', '-1', '1.0', '+1', ' 1', '1 ', 'abc', '9007199254740992'])(
    'fails closed without any network request for invalid schema major %j',
    async (schemaMajor) => {
      const root = await createWorkspaceWithCryptoRepresentative()
      configureTradingDatas(schemaMajor)
      const fetchImpl = vi.fn()
      vi.stubGlobal('fetch', fetchImpl)

      const snapshot = await readTradingAgentSnapshot({
        workspaceRoot: root,
        signalQueueDir: join(root, 'signals'),
        now: new Date('2026-07-20T01:40:00.000Z'),
      })

      expect(fetchImpl).not.toHaveBeenCalled()
      expect(snapshot.marketPulses).toEqual([])
      expect(snapshot.marketPulseCoverage?.entries).toContainEqual(expect.objectContaining({
        market: 'Crypto',
        symbol: 'BTCUSDT',
        status: 'unavailable',
      }))
    },
  )
})
