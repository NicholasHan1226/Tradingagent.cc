import { describe, expect, it } from 'vitest'
import type { FunnelEvent } from '../types/dashboard'
import { createLinkedEvidenceContext, filterEventsByOpportunity } from './linkedEvidenceContext'

const events: FunnelEvent[] = [
  { id: 'a', opportunityId: 'opp-1', symbol: '600519.SH', market: 'A-share', stage: '发现', status: '进入', label: '机会进入', at: '2026-07-11T05:00:00Z', source: 'opportunity_log' },
  { id: 'b', opportunityId: 'opp-1', symbol: '600519.SH', market: 'A-share', stage: '结果', status: '成交', label: '结果写回', at: '2026-07-11T05:10:00Z', source: 'sim_ledger', terminal: true },
  { id: 'c', opportunityId: 'opp-2', symbol: 'BTCUSDT', market: 'Crypto', stage: '风控', status: '拦截', label: '安全拦截', at: '2026-07-11T05:20:00Z', source: 'signal_queue' },
]

describe('linked evidence context', () => {
  it('resolves one selected opportunity from sourced events only', () => {
    expect(createLinkedEvidenceContext(events, 'opp-1')).toEqual(expect.objectContaining({ id: 'opp-1', symbol: '600519.SH', market: 'A股', stage: '结果', result: '结果写回', evidence: '2/5 阶段', eventCount: 2 }))
    expect(createLinkedEvidenceContext(events, 'missing')).toBeNull()
  })

  it('filters the raw event stream by explicit opportunity id', () => {
    expect(filterEventsByOpportunity(events, 'opp-1').map((event) => event.id)).toEqual(['a', 'b'])
    expect(filterEventsByOpportunity(events, null)).toEqual(events)
  })
})
