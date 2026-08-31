import { afterEach, describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from './tradingAgentReadModel'
import { runtimeObservationFixture } from '../test/runtimeObservationFixture'
import {
  TRADING_AGENT_SNAPSHOT_ROUTE,
  createTradingAgentSnapshotClient,
  getTradingAgentSnapshotResponse,
  resolveTradingAgentSnapshotEndpoint,
} from './tradingAgentIntegration'

const snapshot: TradingAgentReadModelSnapshot = {
  mode: 'simulated',
  generatedAt: '2026-07-04T10:00:00.000Z',
  domains: {
    performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    signals: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    holdings: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    decisions: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
    risk: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
  },
  performance: [],
  holdings: [],
  signals: [],
  funnelEvents: [],
  sourceRefs: tradingAgentReadModelSources,
}

describe('TradingAgent integration port', () => {
  it('rejects unsafe optional runtime observations locally without failing core snapshot fields', async () => {
    const unsafe = { ...runtimeObservationFixture(), realTradingEnabled: true }
    const client = createTradingAgentSnapshotClient({ fetcher: async () => new Response(JSON.stringify({ ...snapshot, runtimeObservations: unsafe })) })
    const result = await client.getSnapshot()
    expect(result.runtimeObservations?.entries[0].status).toBe('unavailable')
    expect({ ...result, runtimeObservations: undefined }).toEqual({ ...snapshot, runtimeObservations: undefined })
  })

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('reserves one read-only snapshot route for direct server wiring', () => {
    expect(TRADING_AGENT_SNAPSHOT_ROUTE).toBe('/api/trading-agent/snapshot')
  })

  it('allows a cloud frontend to point at a hosted read-only snapshot API', async () => {
    vi.stubEnv('VITE_TRADING_AGENT_SNAPSHOT_URL', 'https://api.example.com/trading-agent/snapshot')
    const fetcher = vi.fn(async () => new Response(JSON.stringify(snapshot), { status: 200 }))
    const client = createTradingAgentSnapshotClient({ fetcher })

    expect(resolveTradingAgentSnapshotEndpoint()).toBe('https://api.example.com/trading-agent/snapshot')
    await client.getSnapshot()
    expect(fetcher).toHaveBeenCalledWith(
      'https://api.example.com/trading-agent/snapshot',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('fetches and validates a TradingAgent snapshot through the reserved route', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(snapshot), { status: 200 }))
    const client = createTradingAgentSnapshotClient({ fetcher })

    await expect(client.getSnapshot()).resolves.toMatchObject({ mode: 'simulated' })
    expect(fetcher).toHaveBeenCalledWith(TRADING_AGENT_SNAPSHOT_ROUTE, expect.objectContaining({ method: 'GET' }))
  })

  it('rejects a payload that is not the read-model contract', async () => {
    const client = createTradingAgentSnapshotClient({
      fetcher: async () => new Response(JSON.stringify({ ok: true }), { status: 200 }),
    })

    await expect(client.getSnapshot()).rejects.toThrow('TradingAgent snapshot contract is invalid')
  })

  it('wraps server-side snapshot readers as a JSON response without exposing execution', async () => {
    const response = await getTradingAgentSnapshotResponse(async () => snapshot)

    expect(response.status).toBe(200)
    await expect(response.json()).resolves.toMatchObject({ mode: 'simulated' })
  })
})
