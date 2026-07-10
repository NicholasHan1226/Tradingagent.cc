import { describe, expect, it } from 'vitest'
import type { MarketSummary } from '../types/dashboard'
import type { DashboardState } from '../types/status'
import { createEvidenceHealth, createMarketTapeRows } from './marketTapeViewModel'

const summaries: MarketSummary[] = [
  { market: 'A-share', status: 'ready', runtimeState: 'normal', holdingCount: 2, signalCount: 1, tradeCount: 3, styleCount: 1, capitalBase: 50000, pnlAmount: 600, returnPct: 1.2, latestAt: '2026-07-11T04:00:00Z', source: 'ashare ledger', headline: '正常', detail: '已更新' },
  { market: 'Crypto', status: 'partial', runtimeState: 'strategy_wait', holdingCount: 1, signalCount: 0, tradeCount: 0, styleCount: 2, capitalBase: 50000, pnlAmount: -250, returnPct: -0.5, latestAt: '2026-07-11T03:55:00Z', source: 'crypto ledger', headline: '等待', detail: '等待机会' },
]

const domains: DashboardState['domains'] = {
  performance: { status: 'ready', updatedAt: '2026-07-11T04:00:00Z' },
  signals: { status: 'stale', updatedAt: '2026-07-11T03:00:00Z', message: '信号快照滞后' },
  holdings: { status: 'ready', updatedAt: '2026-07-11T04:00:00Z' },
  decisions: { status: 'empty', updatedAt: '2026-07-11T04:00:00Z' },
  risk: { status: 'ready', updatedAt: '2026-07-11T04:00:00Z' },
}

describe('market tape view model', () => {
  it('creates selected market rows with return, holdings and runtime truth', () => {
    const rows = createMarketTapeRows(summaries, 'A-share', '2026-07-11T04:00:00Z')
    const ashare = rows.find((row) => row.market === 'A-share')
    const crypto = rows.find((row) => row.market === 'Crypto')

    expect(ashare).toEqual(expect.objectContaining({ selected: true, returnLabel: '+1.20%', holdingsLabel: '2 持仓', runtimeLabel: '正常' }))
    expect(crypto).toEqual(expect.objectContaining({ selected: false, returnLabel: '-0.50%', runtimeLabel: '策略等待', tone: 'warning' }))
    expect(rows.find((row) => row.market === 'US')).toEqual(expect.objectContaining({ returnLabel: '—', runtimeLabel: '等待数据' }))
  })

  it('builds an all-market CNY return from normalized summaries', () => {
    const all = createMarketTapeRows(summaries, 'All Markets', '2026-07-11T04:00:00Z')[0]

    expect(all).toEqual(expect.objectContaining({ market: 'All Markets', selected: true, returnLabel: '+0.35%', holdingsLabel: '3 持仓' }))
  })

  it('surfaces stale evidence without hiding healthy domains', () => {
    const health = createEvidenceHealth(domains, '2026-07-11T04:00:00Z', summaries[0])

    expect(health.overall).toBe('warning')
    expect(health.items.find((item) => item.domain === 'signals')).toEqual(expect.objectContaining({ label: '信号', state: '快照滞后', tone: 'warning' }))
    expect(health.items.find((item) => item.domain === 'performance')).toEqual(expect.objectContaining({ state: '正常', tone: 'positive' }))
    expect(health.sourceLabel).toBe('ashare ledger')
  })
})
