import { describe, expect, it } from 'vitest'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import type { PerformancePoint, SignalRow } from '../types/dashboard'
import { getSnapshotPerformance, getSnapshotSignals } from './dashboardSnapshot'

const fallbackSignal: SignalRow = {
  symbol: 'AAPL.US',
  name: 'Apple',
  market: 'US',
  method: '趋势跟踪',
  status: 'pending',
  impact: '+12.4',
  confidence: '78%',
  age: '3h',
  reason: '走势确认中',
  next: '等待价格触发',
  steps: 5,
}

const snapshotSignal: SignalRow = {
  ...fallbackSignal,
  symbol: '0700.HK',
  name: 'Tencent',
  market: 'HK',
}

const fallbackPoint: PerformancePoint = {
  day: '5月6日',
  simulated: 0,
  target: 0,
  benchmark: 0,
  opportunity: 0,
}

const snapshotPoint: PerformancePoint = {
  ...fallbackPoint,
  day: '现在',
  simulated: 9.2,
}

function snapshot(partial: Partial<TradingAgentReadModelSnapshot>): TradingAgentReadModelSnapshot {
  return {
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
    ...partial,
  }
}

describe('dashboard snapshot source priority', () => {
  it('uses live TradingAgent signals when the snapshot has rows', () => {
    expect(getSnapshotSignals(snapshot({ signals: [snapshotSignal] }), [fallbackSignal])).toEqual([snapshotSignal])
  })

  it('keeps demo signals only when the snapshot is absent', () => {
    expect(getSnapshotSignals(null, [fallbackSignal])).toEqual([fallbackSignal])
    expect(getSnapshotSignals(snapshot({ signals: [] }), [fallbackSignal])).toEqual([])
  })

  it('uses snapshot performance whenever the snapshot is present', () => {
    expect(getSnapshotPerformance(snapshot({ performance: [snapshotPoint] }), [fallbackPoint])).toEqual([snapshotPoint])
    expect(getSnapshotPerformance(snapshot({ performance: [] }), [fallbackPoint])).toEqual([])
  })
})
