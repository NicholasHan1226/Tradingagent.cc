import { describe, expect, it } from 'vitest'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import { toDashboardApiResponseFromReadModel } from './tradingAgentReadModel'

const baseSnapshot: TradingAgentReadModelSnapshot = {
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

describe('TradingAgent read-model adapter', () => {
  it('normalizes a simulated TradingAgent snapshot into the dashboard API envelope', () => {
    const response = toDashboardApiResponseFromReadModel(baseSnapshot)

    expect(response.mode).toBe('simulated')
    expect(response.status).toBe('ready')
    expect(response.domains.performance.status).toBe('ready')
    expect(baseSnapshot.sourceRefs.positions).toBe('signals/positions/*.json')
    expect(baseSnapshot.sourceRefs.review).toBe('shared/review/daily/daily_brief.jsonl')
  })

  it('keeps stale source health visible instead of flattening it into ready', () => {
    const response = toDashboardApiResponseFromReadModel({
      ...baseSnapshot,
      domains: {
        ...baseSnapshot.domains,
        signals: { status: 'stale', updatedAt: '2026-07-04T09:55:00.000Z', message: '信号队列超过刷新窗口' },
      },
    })

    expect(response.status).toBe('stale')
    expect(response.domains.signals.message).toBe('信号队列超过刷新窗口')
  })
})
