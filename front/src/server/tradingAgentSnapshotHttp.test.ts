import { afterEach, describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import { createTradingAgentSnapshotHttpServer, resolveSnapshotListenHost } from './tradingAgentSnapshotHttp'

const snapshot: TradingAgentReadModelSnapshot = {
  mode: 'simulated',
  generatedAt: '2026-07-04T10:00:00.000Z',
  domains: {
    performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    signals: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    holdings: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
    decisions: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
    risk: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
  },
  performance: [],
  holdings: [],
  signals: [
    {
      symbol: '600519.SH',
      name: '贵州茅台',
      market: 'A-share',
      method: '事件驱动',
      status: 'pending',
      impact: '--',
      confidence: '86%',
      age: '31m',
      reason: '价格和成交量接近走强',
      next: '等待触发条件',
      steps: 5,
    },
  ],
  funnelEvents: [],
  sourceRefs: tradingAgentReadModelSources,
}

const openServers: Array<{ close: () => void }> = []

afterEach(() => {
  for (const server of openServers.splice(0)) server.close()
})

async function listen(server: ReturnType<typeof createTradingAgentSnapshotHttpServer>) {
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  openServers.push(server)
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('server address unavailable')
  return `http://127.0.0.1:${address.port}`
}

describe('TradingAgent cloud snapshot API server', () => {
  it('only permits loopback listen hosts for the internal snapshot API', () => {
    expect(resolveSnapshotListenHost(undefined)).toBe('127.0.0.1')
    expect(resolveSnapshotListenHost('localhost')).toBe('localhost')
    expect(resolveSnapshotListenHost('::1')).toBe('::1')
    expect(() => resolveSnapshotListenHost('0.0.0.0')).toThrowError(
      'TRADING_AGENT_SNAPSHOT_HOST must be a loopback host',
    )
  })

  it('serves both /healthz and /health for operational probes', async () => {
    const baseUrl = await listen(
      createTradingAgentSnapshotHttpServer({
        readSnapshot: async () => snapshot,
      }),
    )

    const healthz = await fetch(`${baseUrl}/healthz`)
    const health = await fetch(`${baseUrl}/health`)

    expect(healthz.status).toBe(200)
    expect(health.status).toBe(200)
    await expect(health.json()).resolves.toMatchObject({ ok: true, service: 'trading-agent-snapshot-api' })
  })

  it('serves a read-only snapshot with no-store cache and restricted CORS', async () => {
    const baseUrl = await listen(
      createTradingAgentSnapshotHttpServer({
        allowedOrigins: ['https://dashboard.example.com'],
        readSnapshot: async () => snapshot,
      }),
    )

    const response = await fetch(`${baseUrl}/api/trading-agent/snapshot`, {
      headers: { Origin: 'https://dashboard.example.com' },
    })

    expect(response.status).toBe(200)
    expect(response.headers.get('cache-control')).toBe('no-store')
    expect(response.headers.get('access-control-allow-origin')).toBe('https://dashboard.example.com')
    await expect(response.json()).resolves.toMatchObject({ mode: 'simulated', signals: [{ symbol: '600519.SH' }] })
  })

  it('rejects wildcard CORS configuration for the single-user dashboard', () => {
    expect(() =>
      createTradingAgentSnapshotHttpServer({
        allowedOrigins: ['*'],
        readSnapshot: async () => snapshot,
      }),
    ).toThrowError('TRADING_AGENT_SNAPSHOT_CORS_ORIGINS must not contain wildcard origins')
  })

  it('requires a bearer token when the API token is configured', async () => {
    const baseUrl = await listen(
      createTradingAgentSnapshotHttpServer({
        allowedOrigins: ['https://dashboard.example.com'],
        apiToken: 'secret-token',
        readSnapshot: async () => snapshot,
      }),
    )

    const unauthorized = await fetch(`${baseUrl}/api/trading-agent/snapshot`, {
      headers: { Origin: 'https://dashboard.example.com' },
    })
    const authorized = await fetch(`${baseUrl}/api/trading-agent/snapshot`, {
      headers: {
        Authorization: 'Bearer secret-token',
        Origin: 'https://dashboard.example.com',
      },
    })

    expect(unauthorized.status).toBe(401)
    expect(authorized.status).toBe(200)
  })

  it('reads verified stock intelligence from an explicitly configured runtime directory', async () => {
    const projectionDir = await mkdtemp(join(tmpdir(), 'copilot-projections-'))
    await mkdir(projectionDir, { recursive: true })
    const projection = {
      symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
      analysis: { symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', generatedAt: '2026-08-02T01:00:00.000Z', evidenceStrength: { value: 72, label: '正式证据强度', semantics: 'typed_evidence_strength_v1', contractVersion: 'v1', sourceRefs: ['source-1'], asOf: '2026-08-02T01:00:00.000Z' }, readiness: { data: 'verified', evidence: 'typed', model: 'ready', action: 'eligible_for_human_review', reasons: ['测试门禁通过'] }, verdict: '等待条件', summary: '正式投影', support: [], oppose: [], buyConditions: ['量价确认'], invalidation: ['结构失效'] },
      source: { datasetId: 'daily', receiptId: 'source-1', receiptSha256: 'b'.repeat(64), dataThrough: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:00:10.000Z', freshness: 'fresh', adjustment: 'forward' },
      marketRules: { board: 'main', lotSize: 100, tPlusOne: true, priceLimitPct: 10, stStatus: 'normal', tradingStatus: 'trading', session: 'closed', corporateActionAdjusted: true },
      quote: { price: 31, previousClose: 30, change: 1, changePct: 3.33, open: 30.5, high: 31.2, low: 30.2, volume: 100, turnoverRate: 1, peTtm: 20, marketCapCny: 1_000_000 },
      company: { exchange: 'SZ', industry: '电网设备', area: '河南', listingDate: '1997-04-18', description: '正式投影' },
      series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] }, forecast: null, events: [],
    }
    const bytes = Buffer.from(JSON.stringify(projection))
    await writeFile(join(projectionDir, '000400.SZ.json'), bytes)
    await writeFile(join(projectionDir, '000400.SZ.receipt.json'), JSON.stringify({
      contractId: 'tradingagent.trading_copilot_stock_projection_receipt.v1', symbol: '000400.SZ', receiptId: 'projection-1',
      projectionSha256: createHash('sha256').update(bytes).digest('hex'), generatedAt: '2026-08-02T01:00:00.000Z', validUntil: '2026-08-03T01:00:00.000Z', verifierId: 'test-verifier', verifierVersion: 'v1',
      sourceReceipts: [{ receiptId: 'source-1', receiptSha256: 'b'.repeat(64) }],
    }))
    const baseUrl = await listen(createTradingAgentSnapshotHttpServer({
      readSnapshot: async () => snapshot,
      copilotProjectionDir: projectionDir,
      copilotProjectionNow: () => new Date('2026-08-02T02:00:00.000Z'),
    }))
    expect((await fetch(`${baseUrl}/api/trading-copilot/stock-intelligence?symbol=000400.SZ`)).status).toBe(200)
  })

  it('rejects browser requests from unapproved origins', async () => {
    const baseUrl = await listen(
      createTradingAgentSnapshotHttpServer({
        allowedOrigins: ['https://dashboard.example.com'],
        readSnapshot: async () => snapshot,
      }),
    )

    const response = await fetch(`${baseUrl}/api/trading-agent/snapshot`, {
      headers: { Origin: 'https://other.example.com' },
    })

    expect(response.status).toBe(403)
  })
})
