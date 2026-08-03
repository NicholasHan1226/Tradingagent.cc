import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer } from 'node:http'
import { createHash } from 'node:crypto'
import { afterEach, describe, expect, it } from 'vitest'
import { createTradingCopilotStockIntelligenceHandler } from './tradingCopilotStockIntelligence'

const servers: ReturnType<typeof createServer>[] = []
afterEach(() => { servers.splice(0).forEach((server) => server.close()) })

describe('TradingCopilot stock intelligence projection', () => {
  it('serves only a verified, symbol-bound read-only artifact', async () => {
    const workspaceRoot = await mkdtemp(join(tmpdir(), 'copilot-stock-'))
    const projectionDir = join(workspaceRoot, 'projection')
    await mkdir(projectionDir)
    const projection = {
      symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
      analysis: { symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', generatedAt: '2026-08-02T01:00:00.000Z', evidenceStrength: { value: 72, label: '正式证据强度', semantics: 'typed_evidence_strength_v1', contractVersion: 'v1', sourceRefs: ['source-1'], asOf: '2026-08-02T01:00:00.000Z' }, readiness: { data: 'verified', evidence: 'typed', model: 'ready', action: 'eligible_for_human_review', reasons: ['测试门禁通过'] }, verdict: '等待条件', summary: '正式投影', support: [], oppose: [], buyConditions: ['量价确认'], invalidation: ['结构失效'] },
      source: {
        transportContract: 'tradingdatas_v1_catalog_query', datasetId: 'daily', receiptId: 'source-1', receiptSha256: 'b'.repeat(64), lineageSha256: 'c'.repeat(64), dataThrough: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:00:10.000Z', freshness: 'fresh', adjustment: 'forward',
        activityAuthority: {
          datasetId: 'daily', market: 'ashare', timezone: 'Asia/Shanghai', dataThrough: '2026-08-02T01:00:00.000Z',
          calendar: { id: 'sse', version: 'v1', sourceDatasetId: 'cn.market.trade_calendar', receiptId: 'calendar-1', receiptSha256: 'd'.repeat(64), lineageSha256: 'e'.repeat(64), calendarSha256: 'f'.repeat(64) },
          session: { state: 'open', asOf: '2026-08-02T01:00:10.000Z' },
          source: { receiptId: 'source-1', receiptSha256: 'b'.repeat(64), lineageSha256: 'c'.repeat(64) },
        },
      },
      marketRules: { board: 'main', lotSize: 100, tPlusOne: true, priceLimitPct: 10, stStatus: 'normal', tradingStatus: 'trading', session: 'closed', corporateActionAdjusted: true },
      quote: { price: 31, previousClose: 30, change: 1, changePct: 3.33, open: 30.5, high: 31.2, low: 30.2, volume: 100, turnoverRate: 1, peTtm: 20, marketCapCny: 1_000_000 },
      company: { exchange: 'SZ', industry: '电网设备', area: '河南', listingDate: '1997-04-18', description: '正式投影' },
      series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] }, forecast: null, events: [],
    }
    const projectionBytes = Buffer.from(JSON.stringify(projection))
    await writeFile(join(projectionDir, '000400.SZ.json'), projectionBytes)
    await writeFile(join(projectionDir, '000400.SZ.receipt.json'), JSON.stringify({
      contractId: 'tradingagent.trading_copilot_stock_projection_receipt.v1', symbol: '000400.SZ', receiptId: 'projection-1',
      projectionSha256: createHash('sha256').update(projectionBytes).digest('hex'), generatedAt: '2026-08-02T01:00:00.000Z', validUntil: '2026-08-03T01:00:00.000Z', verifierId: 'test-verifier', verifierVersion: 'v1',
      sourceReceipts: [{ receiptId: 'source-1', receiptSha256: 'b'.repeat(64) }, { receiptId: 'calendar-1', receiptSha256: 'd'.repeat(64) }],
    }))
    const handler = createTradingCopilotStockIntelligenceHandler({ workspaceRoot, projectionDir, now: () => new Date('2026-08-02T02:00:00.000Z') })
    const server = createServer((req, res) => { void handler(req, res) })
    servers.push(server)
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('server_address_unavailable')
    const base = `http://127.0.0.1:${address.port}`
    expect((await fetch(`${base}/api/trading-copilot/stock-intelligence?symbol=000400.SZ`)).status).toBe(200)
    expect((await fetch(`${base}/api/trading-copilot/stock-intelligence?symbol=../../secret`)).status).toBe(400)
    expect((await fetch(`${base}/api/trading-copilot/stock-intelligence?symbol=000400.SZ`, { method: 'POST' })).status).toBe(405)
  })
})
