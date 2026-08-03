import { mkdtemp, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { createTradingCopilotTrackingUniverseHandler } from './tradingCopilotTrackingUniverse'

const servers: ReturnType<typeof createServer>[] = []
afterEach(() => { servers.splice(0).forEach((server) => server.close()) })

const universe = {
  contractId: 'tradingagent.trading_copilot_tracking_universe.v1',
  generatedAt: '2026-08-03T01:00:00.000Z',
  items: [{ symbol: '000400.SZ', name: '许继电气' }],
}

describe('TradingCopilot tracking universe projection', () => {
  it('serves only a verified GET-only regular file', async () => {
    const workspaceRoot = await mkdtemp(join(tmpdir(), 'copilot-universe-'))
    const path = join(workspaceRoot, 'tracking-universe.json')
    await writeFile(path, JSON.stringify(universe))
    const server = createServer((req, res) => { void createTradingCopilotTrackingUniverseHandler({ workspaceRoot, trackingUniversePath: path })(req, res) })
    servers.push(server)
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('server_address_unavailable')
    const base = `http://127.0.0.1:${address.port}`

    expect((await fetch(`${base}/api/trading-copilot/tracking-universe`)).status).toBe(200)
    expect((await fetch(`${base}/api/trading-copilot/tracking-universe`, { method: 'POST' })).status).toBe(405)
  })

  it('fails closed for an invalid or linked projection', async () => {
    const workspaceRoot = await mkdtemp(join(tmpdir(), 'copilot-universe-'))
    const target = join(workspaceRoot, 'target.json')
    const link = join(workspaceRoot, 'tracking-universe.json')
    await writeFile(target, JSON.stringify(universe))
    await symlink(target, link)
    const handler = createTradingCopilotTrackingUniverseHandler({ workspaceRoot, trackingUniversePath: link })
    const server = createServer((req, res) => { void handler(req, res) })
    servers.push(server)
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('server_address_unavailable')
    expect((await fetch(`http://127.0.0.1:${address.port}/api/trading-copilot/tracking-universe`)).status).toBe(404)
  })
})
