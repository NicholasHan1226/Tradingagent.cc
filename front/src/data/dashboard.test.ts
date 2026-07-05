import { describe, expect, it } from 'vitest'
import { allocationData, holdings, markets, signals } from './dashboard'

describe('dashboard fallback data', () => {
  it('excludes suspended HK rows from the default dashboard fallback', () => {
    expect(markets).not.toContain('HK')
    expect(signals.every((signal) => signal.market !== 'HK')).toBe(true)
    expect(holdings.every((holding) => holding.market !== 'HK')).toBe(true)
    expect(allocationData.every((item) => item.name !== '港股')).toBe(true)
  })
})
