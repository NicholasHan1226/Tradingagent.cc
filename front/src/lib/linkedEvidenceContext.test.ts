import { describe, expect, it } from 'vitest'
import type { FunnelEvent, HoldingRow, SignalRow } from '../types/dashboard'
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

  it('attributes signals, holdings and PnL only through the same explicit opportunity id', () => {
    const signals: SignalRow[] = [
      { market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-1', method: '候选', status: 'executed', impact: '—', confidence: '—', age: '1m', reason: '—', next: '—', steps: 5 },
      { market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-2', method: '候选', status: 'executed', impact: '—', confidence: '—', age: '1m', reason: '—', next: '—', steps: 5 },
    ]
    const holdings: HoldingRow[] = [
      { market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-1', weight: '¥1,000', pnl: '+¥10', realizedPnl: 12.5, unrealizedPnl: -2.5, risk: '正常', role: '模拟盘持仓' },
      { market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-2', weight: '¥1,000', pnl: '+¥99', realizedPnl: 99, risk: '正常', role: '模拟盘持仓' },
    ]

    expect(createLinkedEvidenceContext(events, 'opp-1', signals, holdings)).toEqual(expect.objectContaining({ signalCount: 1, holdingCount: 1, attributablePnl: 10 }))
  })

  it('keeps a frozen legacy opportunity namespace detached from current signal and PnL attribution', () => {
    const legacyEvents: FunnelEvent[] = [{
      ...events[0],
      source: 'legacy_frozen_opportunity_log',
    }]
    const signals: SignalRow[] = [{
      market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-1', method: '候选', status: 'executed', impact: '—', confidence: '—', age: '1m', reason: '—', next: '—', steps: 5,
    }]
    const holdings: HoldingRow[] = [{
      market: 'A-share', symbol: '600519.SH', name: '贵州茅台', opportunityId: 'opp-1', weight: '¥1,000', pnl: '+¥10', realizedPnl: 10, risk: '正常', role: '模拟盘持仓',
    }]

    expect(createLinkedEvidenceContext(legacyEvents, 'opp-1', signals, holdings)).toEqual(expect.objectContaining({
      legacyFrozen: true,
      signalCount: 0,
      holdingCount: 0,
      attributablePnl: undefined,
    }))
  })
})
