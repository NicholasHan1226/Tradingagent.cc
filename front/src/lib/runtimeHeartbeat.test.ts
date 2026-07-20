import { describe, expect, it } from 'vitest'
import type { FunnelEvent, MarketSummary, SignalRow } from '../types/dashboard'
import type { DashboardState } from '../types/status'
import { createRuntimeHeartbeat, translateTerminalValue } from './runtimeHeartbeat'

const now = new Date('2026-07-11T05:30:00.000Z')
const domains: DashboardState['domains'] = {
  performance: { status: 'ready', updatedAt: '2026-07-11T05:29:50.000Z' },
  signals: { status: 'ready', updatedAt: '2026-07-11T05:29:50.000Z' },
  holdings: { status: 'empty', updatedAt: '2026-07-11T05:29:50.000Z' },
  decisions: { status: 'ready', updatedAt: '2026-07-11T05:29:50.000Z' },
  risk: { status: 'ready', updatedAt: '2026-07-11T05:29:50.000Z' },
}
const event: FunnelEvent = {
  id: 'event-1', symbol: 'BTC-USDT', market: 'Crypto', opportunityId: 'opp-1',
  stage: '结果', status: '成交', label: '结果写回', source: 'sim_ledger', at: '2026-07-11T05:20:00.000Z',
}
const pending: SignalRow = {
  symbol: 'BTC-USDT', name: '比特币', market: 'Crypto', method: 'buy', status: 'pending',
  impact: '—', confidence: '70%', age: '2分钟', reason: '等待结果', next: '继续观察', steps: 4,
}

function build(overrides: Partial<Parameters<typeof createRuntimeHeartbeat>[0]> = {}) {
  return createRuntimeHeartbeat({
    domains,
    generatedAt: '2026-07-11T05:29:50.000Z',
    funnelEvents: [event],
    marketSummary: undefined,
    now,
    signals: [],
    ...overrides,
  })
}

describe('runtime heartbeat', () => {
  it('distinguishes a healthy idle scheduler from active processing', () => {
    expect(build()).toMatchObject({ state: 'idle', headline: '调度正常 · 当前空闲', runningCount: 0, latestEventLabel: '最近事件 10分钟前' })
    expect(build({ signals: [pending] })).toMatchObject({ state: 'live', headline: '自动过程运行中 · 1项', runningCount: 1 })
  })

  it('does not use frozen legacy opportunity history as a current heartbeat event', () => {
    expect(build({
      funnelEvents: [{ ...event, source: 'legacy_frozen_opportunity_log' }],
    })).toMatchObject({ latestEventLabel: '尚无过程事件' })
  })

  it('lets degraded and stale evidence override idle wording', () => {
    expect(build({ domains: { ...domains, signals: { status: 'error', updatedAt: '2026-07-11T05:29:50.000Z' } } })).toMatchObject({ state: 'degraded', headline: '证据读取异常 · 需要关注' })
    expect(build({ generatedAt: '2026-07-11T04:00:00.000Z' })).toMatchObject({ state: 'stale', headline: '快照滞后 · 等待更新' })
  })

  it('treats a market execution fault as degraded', () => {
    const marketSummary = { executionFault: true } as MarketSummary
    expect(build({ marketSummary })).toMatchObject({ state: 'degraded' })
  })

  it('translates internal terminal values before rendering', () => {
    expect(translateTerminalValue('buy')).toBe('买入观察')
    expect(translateTerminalValue('sell')).toBe('卖出观察')
    expect(translateTerminalValue('empty')).toBe('等待数据')
    expect(translateTerminalValue('sim_ledger')).toBe('模拟账本')
  })
})
