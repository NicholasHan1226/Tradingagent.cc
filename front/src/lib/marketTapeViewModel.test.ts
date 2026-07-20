import { describe, expect, it } from 'vitest'
import type { MarketPulse, MarketPulseCoverage, MarketPulseCoverageObservation, MarketSummary } from '../types/dashboard'
import type { DashboardState } from '../types/status'
import { createEvidenceHealth, createMarketPulseHealth, createMarketTapeRows } from './marketTapeViewModel'

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

const pulses: MarketPulse[] = [{ market: 'A-share', symbol: '600519.SH', lastPrice: 1424.1, changePct: 1, high: 1430, low: 1400, volume: 2000, updatedAt: '2026-07-11T04:00:00Z', freshness: 'live', points: [1410, 1414, 1424.1], source: 'TradingDatas V1' }]
const pulseCoverage: MarketPulseCoverage = {
  cacheState: 'cached', fetchedAt: '2026-07-11T04:00:00Z', requestedCount: 2, sourcedCount: 1, sourceLatencyMs: 18,
  entries: [
    { market: 'A-share', symbol: '600519.SH', status: 'sourced' },
    { market: 'Crypto', symbol: 'BTCUSDT', status: 'unavailable' },
    { market: 'US', status: 'no_representative' }, { market: 'HK', status: 'no_representative' }, { market: 'PM', status: 'no_representative' }, { market: 'CNFutures', status: 'no_representative' },
  ],
}
const coverageHistory: MarketPulseCoverageObservation[] = [
  { ...pulseCoverage, entries: pulseCoverage.entries.map((entry) => ({ ...entry })), fetchedAt: '2026-07-11T03:30:00Z' },
  { ...pulseCoverage, entries: pulseCoverage.entries.map((entry) => ({ ...entry })), fetchedAt: '2026-07-11T04:00:00Z' },
]

describe('market tape view model', () => {
  it('creates selected market rows with return, holdings and runtime truth', () => {
    const rows = createMarketTapeRows(summaries, 'A-share', '2026-07-11T04:00:00Z')
    const ashare = rows.find((row) => row.market === 'A-share')
    const crypto = rows.find((row) => row.market === 'Crypto')

    expect(ashare).toEqual(expect.objectContaining({ selected: true, returnLabel: '+1.20%', holdingsLabel: '2 持仓', runtimeLabel: '正常' }))
    expect(crypto).toEqual(expect.objectContaining({ selected: false, returnLabel: '-0.50%', runtimeLabel: '策略等待', tone: 'warning' }))
    expect(rows.find((row) => row.market === 'US')).toEqual(expect.objectContaining({ returnLabel: '—', runtimeLabel: '等待数据' }))
  })

  it('builds an all-market row with non-monetary aggregates only', () => {
    const all = createMarketTapeRows(summaries, 'All Markets', '2026-07-11T04:00:00Z')[0]

    // Monetary aggregation is decommissioned; All Markets shows '—' for return
    expect(all).toEqual(expect.objectContaining({ market: 'All Markets', selected: true, returnLabel: '—', holdingsLabel: '3 持仓', runtimeLabel: '正常', tone: 'positive' }))
  })

  it('refuses to aggregate monetary return across markets in All Markets row', () => {
    const dualSummaries: MarketSummary[] = [
      { market: 'A-share', status: 'ready', runtimeState: 'normal', holdingCount: 2, signalCount: 1, tradeCount: 3, styleCount: 1, capitalBase: 50000, pnlAmount: 600, returnPct: 1.2, latestAt: '2026-07-11T04:00:00Z', source: 'ashare', headline: '正常', detail: '已更新' },
      { market: 'CNFutures', status: 'ready', runtimeState: 'normal', holdingCount: 1, signalCount: 1, tradeCount: 2, styleCount: 1, capitalBase: 50000, pnlAmount: -300, returnPct: -0.6, latestAt: '2026-07-11T04:00:00Z', source: 'cnfutures', headline: '正常', detail: '已更新' },
    ]
    const all = createMarketTapeRows(dualSummaries, 'All Markets', '2026-07-11T04:00:00Z')[0]

    // Must NOT show a combined monetary return; individual market rows carry their own
    expect(all.returnLabel).toBe('—')
    // Non-monetary counts may be aggregated
    expect(all.holdingsLabel).toBe('3 持仓')
  })

  it('never substitutes one market monetary return when the other is missing', () => {
    const singleSummary: MarketSummary[] = [
      { market: 'A-share', status: 'ready', runtimeState: 'normal', holdingCount: 2, signalCount: 1, tradeCount: 3, styleCount: 1, capitalBase: 50000, pnlAmount: 600, returnPct: 1.2, latestAt: '2026-07-11T04:00:00Z', source: 'ashare', headline: '正常', detail: '已更新' },
    ]
    const all = createMarketTapeRows(singleSummary, 'All Markets', '2026-07-11T04:00:00Z')[0]

    // Should not show +1.20% as the "All Markets" return — that's A-share's, not a composite
    expect(all.returnLabel).toBe('—')
  })

  it('adds a real representative instrument pulse without fabricating missing markets', () => {
    const rows = createMarketTapeRows(summaries, 'A-share', '2026-07-11T04:00:00Z', pulses)

    expect(rows.find((row) => row.market === 'A-share')?.pulse).toEqual(expect.objectContaining({ symbol: '600519.SH', priceLabel: '1,424.10', changeLabel: '+1.00%', freshness: 'live', points: [1410, 1414, 1424.1] }))
    expect(rows.find((row) => row.market === 'US')?.pulse).toBeUndefined()
  })

  it('surfaces stale evidence without hiding healthy domains', () => {
    const health = createEvidenceHealth(domains, '2026-07-11T04:00:00Z', summaries[0])

    expect(health.overall).toBe('warning')
    expect(health.items.find((item) => item.domain === 'signals')).toEqual(expect.objectContaining({ label: '信号', state: '快照滞后', tone: 'warning' }))
    expect(health.items.find((item) => item.domain === 'performance')).toEqual(expect.objectContaining({ state: '正常', tone: 'positive' }))
    expect(health.sourceLabel).toBe('ashare ledger')
  })

  it('summarizes pulse coverage instead of pretending unmapped markets have prices', () => {
    expect(createMarketPulseHealth(pulseCoverage, coverageHistory)).toEqual(expect.objectContaining({
      headline: '1/2 已取到',
      detail: expect.stringContaining('4 市场待映射'),
      traceLabel: '轨迹 2',
      traceDetail: expect.stringContaining('近 2 次来源观测'),
      tone: 'warning',
    }))
  })
})
