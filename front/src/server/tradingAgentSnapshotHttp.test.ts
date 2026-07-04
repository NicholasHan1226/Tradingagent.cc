import { afterEach, describe, expect, it } from 'vitest'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import { createTradingAgentSnapshotHttpServer } from './tradingAgentSnapshotHttp'

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
      symbol: '0700.HK',
      name: 'Tencent',
      market: 'HK',
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
    await expect(response.json()).resolves.toMatchObject({ mode: 'simulated', signals: [{ symbol: '0700.HK' }] })
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
