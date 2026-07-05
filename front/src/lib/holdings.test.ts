import { describe, expect, it } from 'vitest'
import { getMarketAllocation, summarizeHoldingExposure } from './holdings'
import type { HoldingRow } from '../types/dashboard'

const baseHolding: HoldingRow = {
  symbol: '600519.SH',
  name: '贵州茅台',
  market: 'A-share',
  weight: '10%',
  pnl: '+$10',
  risk: '正常',
  role: '测试持仓',
}

describe('holding exposure helpers', () => {
  it('keeps percentage holdings as total weight', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '12.8%' },
      { ...baseHolding, symbol: 'AAPL.US', market: 'US', weight: '10.6%' },
    ])

    expect(summary).toMatchObject({
      label: '总仓位',
      value: '23.4%',
      mode: 'percent',
    })
  })

  it('summarizes dollar holdings as recorded amount', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '$1,200' },
      { ...baseHolding, symbol: 'AAPL.US', market: 'US', weight: '$800' },
    ])

    expect(summary).toMatchObject({
      label: '持仓金额',
      value: '$2,000',
      mode: 'amount',
    })
  })

  it('turns dollar holdings into market allocation shares', () => {
    const allocation = getMarketAllocation([
      { ...baseHolding, weight: '$1,200' },
      { ...baseHolding, symbol: 'AAPL.US', market: 'US', weight: '$800' },
    ])

    expect(allocation).toEqual([
      { name: 'A股', value: 60 },
      { name: '美股', value: 40 },
    ])
  })

  it('does not pretend mixed amount and percent holdings share one allocation scale', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '$1,200' },
      { ...baseHolding, symbol: 'AAPL.US', market: 'US', weight: '10%' },
    ])
    const allocation = getMarketAllocation([
      { ...baseHolding, weight: '$1,200' },
      { ...baseHolding, symbol: 'AAPL.US', market: 'US', weight: '10%' },
    ])

    expect(summary).toMatchObject({
      label: '持仓记录',
      value: '2',
      mode: 'mixed',
    })
    expect(allocation).toEqual([{ name: '记录待统一', value: 100 }])
  })
})
