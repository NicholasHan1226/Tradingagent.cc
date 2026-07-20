import { describe, expect, it } from 'vitest'
import type { FunnelEvent } from '../types/dashboard'
import { createProcessCycles } from './processCycleViewModel'

const events: FunnelEvent[] = [
  { id: 'opp1-risk', opportunityId: 'opp-1', sequence: 3, symbol: '600519.SH', market: 'A-share', stage: '风控', status: '通过', label: '风控通过', at: '2026-07-11T05:06:00Z', source: 'signal_queue', latencyMinutes: 2 },
  { id: 'opp2-result', opportunityId: 'opp-2', sequence: 2, symbol: 'BTC-USDT', market: 'Crypto', stage: '结果', status: '拦截', label: '安全拦截', at: '2026-07-11T05:20:00Z', source: 'signal_queue', reason: '波动超过边界', terminal: true },
  { id: 'opp1-result', opportunityId: 'opp-1', sequence: 5, symbol: '600519.SH', market: 'A-share', stage: '结果', status: '成交', label: '结果写回', at: '2026-07-11T05:10:00Z', source: 'sim_ledger', latencyMinutes: 4, terminal: true },
  { id: 'opp1-discovered', opportunityId: 'opp-1', sequence: 1, symbol: '600519.SH', market: 'A-share', stage: '发现', status: '进入', label: '机会进入', at: '2026-07-11T05:00:00Z', source: 'opportunity_log' },
]

describe('process cycle view model', () => {
  it('groups events by opportunity and orders cycles by their latest sourced event', () => {
    const rows = createProcessCycles(events)

    expect(rows.map((row) => row.id)).toEqual(['opp-2', 'opp-1'])
    expect(rows[1]).toMatchObject({
      symbol: '600519.SH', market: 'A股', result: '结果写回', source: '模拟账本',
      latency: '10分钟', evidence: '3/5 阶段', reason: '—', updatedAt: '07/11 13:10',
    })
    expect(rows[1].stages.map((stage) => [stage.label, stage.state])).toEqual([
      ['发现', 'complete'], ['研判', 'missing'], ['风控', 'complete'], ['待确认', 'missing'], ['结果', 'current'],
    ])
  })

  it('does not infer missing stages or leak raw source codes', () => {
    const [row] = createProcessCycles([events[1]])
    expect(row.evidence).toBe('1/5 阶段')
    expect(row.source).toBe('队列状态投影')
    expect(JSON.stringify(row)).not.toContain('signal_queue')
  })

  it('keeps retired funnel rows visibly frozen instead of presenting them as current events', () => {
    const [row] = createProcessCycles([
      { ...events[0], source: 'legacy_frozen_opportunity_log' },
    ])

    expect(row.source).toBe('旧漏斗冻结历史')
  })
})
