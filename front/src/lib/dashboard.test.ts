import { describe, expect, it } from 'vitest'
import {
  getActionableSignals,
  getClosedSignals,
  getLivePerformanceData,
  getVisibleHoldings,
  getSignalFunnel,
  getVisibleSignals,
} from './dashboard'
import type { HoldingRow, PerformancePoint, SignalRow } from '../types/dashboard'

const rows: SignalRow[] = [
  {
    symbol: '0700.HK',
    name: '腾讯',
    market: 'HK',
    method: '事件机会',
    status: 'pending',
    impact: '--',
    confidence: '86%',
    age: '31分钟',
    reason: '价格接近走强',
    next: '继续观察',
    steps: 5,
  },
  {
    symbol: 'BTC-USD',
    name: '比特币',
    market: 'Crypto',
    method: '突破机会',
    status: 'blocked',
    impact: '+23.7',
    confidence: '74%',
    age: '4小时',
    reason: '波动太大',
    next: '等待风险下降',
    steps: 4,
  },
  {
    symbol: 'AAPL.US',
    name: '苹果',
    market: 'US',
    method: '顺势跟踪',
    status: 'executed',
    impact: '+12.4',
    confidence: '86%',
    age: '3小时',
    reason: '趋势延续',
    next: '保留仓位',
    steps: 6,
  },
]

describe('dashboard view rules', () => {
  it('keeps opportunity pages focused on actionable rows', () => {
    expect(getActionableSignals(rows).map((signal) => signal.symbol)).toEqual(['0700.HK', 'BTC-USD'])
  })

  it('keeps review pages focused on closed rows', () => {
    expect(getClosedSignals(rows).map((signal) => signal.symbol)).toEqual(['AAPL.US'])
  })

  it('keeps market filters strict when a market has no matching rows', () => {
    expect(getVisibleSignals(rows, 'A-share')).toHaveLength(0)
  })

  it('filters holdings with the same market boundary as signals', () => {
    const holdings: HoldingRow[] = [
      { symbol: '600519.SH', name: '贵州茅台', market: 'A-share', weight: '¥1万', pnl: '+¥20', risk: '正常', role: '模拟盘持仓' },
      { symbol: 'BTC-USD', name: '比特币', market: 'Crypto', weight: '$800', pnl: '+$12', risk: '正常', role: 'Grid 持仓' },
    ]

    expect(getVisibleHoldings(holdings, 'Crypto').map((holding) => holding.symbol)).toEqual(['BTC-USD'])
  })

  it('updates only the latest live performance point', () => {
    const performanceRows: PerformancePoint[] = [
      { day: '5月6日', simulated: 0.2, target: 0, benchmark: 0.1, opportunity: -0.2 },
      { day: '现在', simulated: 9.42, target: 8, benchmark: 2.15, opportunity: -2.55 },
    ]

    const result = getLivePerformanceData(new Date('2026-07-04T10:00:00+08:00'), performanceRows, true)

    expect(result[0]).toBe(performanceRows[0])
    expect(result[1]).not.toBe(performanceRows[1])
    expect(result[1].target).toBe(8)
  })

  it('marks executed-only ledger rows as replay rather than a live screening funnel', () => {
    const funnel = getSignalFunnel([
      {
        ...rows[2],
        stage: '成交',
        stageEvidence: 'replay',
      },
    ])

    expect(funnel.mode).toBe('replay')
    expect(funnel.hasScreeningEvidence).toBe(false)
    expect(funnel.stageDrops).toEqual([0, 0, 0, 0, 0])
  })

  it('marks dropped or partially staged signals as a screening funnel', () => {
    const funnel = getSignalFunnel([
      {
        ...rows[0],
        stage: '待执行',
        stageEvidence: 'partial',
      },
      {
        ...rows[1],
        stage: '拒绝',
        stageEvidence: 'partial',
      },
    ])

    expect(funnel.mode).toBe('screening')
    expect(funnel.hasScreeningEvidence).toBe(true)
    expect(funnel.stageDrops.some((drop) => drop > 0)).toBe(true)
  })
})
