import { describe, expect, it } from 'vitest'
import { allocationData, holdings, markets, signals } from './dashboard'

describe('dashboard fallback data', () => {
  it('contains only the three active market lanes and shared context', () => {
    expect(markets).toEqual(['All Markets', 'A-share', 'CNFutures', 'Crypto'])
    expect(new Set(signals.map((signal) => signal.market))).toEqual(new Set(['A-share', 'CNFutures', 'Crypto']))
    expect(new Set(holdings.map((holding) => holding.market))).toEqual(new Set(['A-share', 'CNFutures', 'Crypto']))
    expect(allocationData.map((item) => item.name)).toEqual(['A股', '中国期货', '加密', '现金'])
  })
})
