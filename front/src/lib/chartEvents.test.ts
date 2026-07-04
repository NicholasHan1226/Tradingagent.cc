import { describe, expect, it } from 'vitest'
import { deriveChartEvents } from './chartEvents'
import type { PerformancePoint, SignalRow } from '../types/dashboard'

const performance: PerformancePoint[] = [
  { day: '5月24日', simulated: 4.0, target: 3.8, benchmark: 1.1, opportunity: -3.0 },
  { day: '5月26日', simulated: 5.0, target: 4.2, benchmark: 1.2, opportunity: -3.6 },
  { day: '5月28日', simulated: 6.5, target: 4.7, benchmark: 1.3, opportunity: -4.1 },
  { day: '5月30日', simulated: 7.6, target: 5.2, benchmark: 1.4, opportunity: -4.2 },
  { day: '6月1日', simulated: 8.2, target: 5.7, benchmark: 1.5, opportunity: -4.0 },
  { day: '6月3日', simulated: 8.4, target: 6.1, benchmark: 1.6, opportunity: -3.8 },
  { day: '6月5日', simulated: 9.2, target: 6.5, benchmark: 1.7, opportunity: -3.9 },
  { day: '6月7日', simulated: 9.8, target: 6.9, benchmark: 1.8, opportunity: -4.2 },
  { day: '6月9日', simulated: 10.6, target: 7.3, benchmark: 1.9, opportunity: -3.9 },
  { day: '6月11日', simulated: 11.4, target: 7.7, benchmark: 2.0, opportunity: -3.7 },
  { day: '6月13日', simulated: 12.6, target: 8.1, benchmark: 2.1, opportunity: -3.5 },
  { day: '6月15日', simulated: 12.1, target: 8.5, benchmark: 2.0, opportunity: -3.6 },
  { day: '6月17日', simulated: 11.6, target: 8.8, benchmark: 2.1, opportunity: -3.3 },
  { day: '现在', simulated: 11.7, target: 8.0, benchmark: 2.15, opportunity: -2.55 },
]

const signals: SignalRow[] = [
  {
    symbol: '0700.HK',
    name: '腾讯',
    market: 'HK',
    method: '事件机会',
    status: 'pending',
    impact: '--',
    confidence: '86%',
    age: '31分钟',
    reason: '财报预期和资金流正在靠近',
    next: '等价格和成交量再走强',
    steps: 5,
  },
]

describe('deriveChartEvents', () => {
  it('turns performance movement and current signals into navigable events', () => {
    const events = deriveChartEvents(performance, signals)

    expect(events).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ day: '5月28日', targetPage: '决策' }),
        expect.objectContaining({ day: '6月17日', targetPage: '风险' }),
        expect.objectContaining({ day: '现在', targetPage: '机会' }),
      ]),
    )
  })

  it('keeps the event list compact for the chart surface', () => {
    expect(deriveChartEvents(performance, signals)).toHaveLength(3)
  })
})
