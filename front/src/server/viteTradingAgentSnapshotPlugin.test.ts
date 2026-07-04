import { describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import { createTradingAgentSnapshotMiddleware } from './viteTradingAgentSnapshotPlugin'

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
  sourceRefs: tradingAgentReadModelSources,
}

function responseRecorder() {
  const headers = new Map<string, string | number | readonly string[]>()
  let body = ''

  return {
    res: {
      statusCode: 200,
      setHeader: (key: string, value: string | number | readonly string[]) => headers.set(key, value),
      end: (value?: unknown) => {
        body = String(value ?? '')
      },
    },
    getBody: () => body,
    getHeader: (key: string) => headers.get(key),
  }
}

describe('Vite TradingAgent snapshot middleware', () => {
  it('serves the read-only snapshot route with no-store JSON', async () => {
    const readSnapshot = vi.fn(async () => snapshot)
    const middleware = createTradingAgentSnapshotMiddleware({ readSnapshot })
    const recorder = responseRecorder()

    await middleware({ method: 'GET', url: '/api/trading-agent/snapshot' }, recorder.res, vi.fn())

    expect(readSnapshot).toHaveBeenCalledTimes(1)
    expect(recorder.res.statusCode).toBe(200)
    expect(recorder.getHeader('Cache-Control')).toBe('no-store')
    expect(JSON.parse(recorder.getBody())).toMatchObject({ mode: 'simulated' })
  })

  it('passes through unrelated Vite requests', async () => {
    const next = vi.fn()
    const middleware = createTradingAgentSnapshotMiddleware({ readSnapshot: async () => snapshot })
    const recorder = responseRecorder()

    await middleware({ method: 'GET', url: '/src/App.tsx' }, recorder.res, next)

    expect(next).toHaveBeenCalledTimes(1)
  })
})
