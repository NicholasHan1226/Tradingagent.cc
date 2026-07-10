import { describe, expect, it } from 'vitest'
import type { FunnelEvent } from '../types/dashboard'
import { createProcessEventRows } from './processEventViewModel'

const earlier: FunnelEvent = {
  id: 'earlier', symbol: '600519.SH', market: 'A-share', sequence: 1,
  stage: '发现', status: '进入', label: '机会进入', at: '2026-07-11T03:01:00Z', source: 'signal_queue',
}
const later: FunnelEvent = {
  id: 'later', symbol: '600519.SH', market: 'A-share', sequence: 2,
  stage: '研判', status: '通过', label: '研究通过', at: '2026-07-11T03:05:00Z', source: 'opportunity_log', latencyMinutes: 4, reason: '评分达到阈值',
}

describe('process event view model', () => {
  it('orders events by timestamp descending and translates source and latency', () => {
    const rows = createProcessEventRows([earlier, later])

    expect(rows.map((row) => row.id)).toEqual(['later', 'earlier'])
    expect(rows[0]).toMatchObject({ source: '机会事件', latency: '4分钟', reason: '评分达到阈值' })
  })

  it('uses sequence as a fallback and never invents missing evidence', () => {
    const rows = createProcessEventRows([
      { ...earlier, id: 'one', at: undefined, sequence: 1 },
      { ...later, id: 'two', at: undefined, sequence: 2, latencyMinutes: undefined, reason: undefined },
    ])

    expect(rows.map((row) => row.id)).toEqual(['two', 'one'])
    expect(rows[0]).toMatchObject({ timestamp: '—', latency: '—', reason: '—' })
  })
})
