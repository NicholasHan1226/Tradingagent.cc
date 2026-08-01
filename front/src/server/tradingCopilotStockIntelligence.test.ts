import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer } from 'node:http'
import { afterEach, describe, expect, it } from 'vitest'
import { createTradingCopilotStockIntelligenceHandler } from './tradingCopilotStockIntelligence'

const servers: ReturnType<typeof createServer>[] = []
afterEach(() => { servers.splice(0).forEach((server) => server.close()) })

describe('TradingCopilot stock intelligence projection', () => {
  it('serves only a verified, symbol-bound read-only artifact', async () => {
    const workspaceRoot = await mkdtemp(join(tmpdir(), 'copilot-stock-'))
    const projectionDir = join(workspaceRoot, 'projection')
    await mkdir(projectionDir)
    await writeFile(join(projectionDir, '000400.SZ.json'), JSON.stringify({
      symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
      quote: { price: 31, previousClose: 30, change: 1, changePct: 3.33, open: 30.5, high: 31.2, low: 30.2, volume: 100, turnoverRate: 1, peTtm: 20, marketCapCny: 1_000_000 },
      company: { exchange: 'SZ', industry: '电网设备', area: '河南', listingDate: '1997-04-18', description: '正式投影' },
      series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] }, forecast: null, events: [],
    }))
    const handler = createTradingCopilotStockIntelligenceHandler({ workspaceRoot, projectionDir })
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
