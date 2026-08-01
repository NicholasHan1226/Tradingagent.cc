import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { emptyTradingCopilotState } from '../copilot/types'
import { createTradingAgentSnapshotHttpServer } from './tradingAgentSnapshotHttp'

const servers: Array<ReturnType<typeof createTradingAgentSnapshotHttpServer>> = []
const tempDirs: string[] = []

afterEach(async () => {
  for (const server of servers.splice(0)) server.close()
  await Promise.all(tempDirs.splice(0).map((path) => rm(path, { force: true, recursive: true })))
})

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'trading-copilot-'))
  tempDirs.push(root)
  const statePath = join(root, 'state-events.jsonl')
  const server = createTradingAgentSnapshotHttpServer({ copilotStatePath: statePath, workspaceRoot: root })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  servers.push(server)
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('missing address')
  return { baseUrl: `http://127.0.0.1:${address.port}`, statePath }
}

describe('TradingCopilot state API', () => {
  it('persists user-declared state as append-only events and reads the latest state', async () => {
    const { baseUrl, statePath } = await fixture()
    const state = emptyTradingCopilotState('2026-08-01T00:00:00.000Z')
    state.account = { declaredCapitalCny: 100000, availableCashCny: 80000, updatedAt: state.updatedAt }

    const saved = await fetch(`${baseUrl}/api/trading-copilot/state`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state),
    })
    const loaded = await fetch(`${baseUrl}/api/trading-copilot/state`)

    expect(saved.status).toBe(200)
    await expect(loaded.json()).resolves.toMatchObject({ source: 'user_declared', account: { declaredCapitalCny: 100000 } })
    const event = JSON.parse((await readFile(statePath, 'utf8')).trim())
    expect(event).toMatchObject({ previousSha256: null, state: { ownerId: 'nicholas' } })
    expect(event.stateSha256).toMatch(/^[a-f0-9]{64}$/)
  })

  it('rejects invalid quantities and does not treat decisions as orders', async () => {
    const { baseUrl } = await fixture()
    const state = emptyTradingCopilotState('2026-08-01T00:00:00.000Z')
    state.holdings = [{ symbol: '000400.SZ', name: '许继电气', quantity: 100, sellableQuantity: 200, averageCost: 30, updatedAt: state.updatedAt }]
    const response = await fetch(`${baseUrl}/api/trading-copilot/state`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state),
    })
    expect(response.status).toBe(400)
    await expect(response.json()).resolves.toMatchObject({ error: 'Holding quantities are invalid' })
  })
})
