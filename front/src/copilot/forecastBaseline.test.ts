import { describe, expect, it } from 'vitest'
import { buildLinearBaseline } from './forecastBaseline'

describe('linear forecast baseline', () => {
  it('is deterministic, bounded, and emits envelopes without probabilities', () => {
    const prices = [10, 10.1, 10.2, 10.15, 10.3, 10.35, 10.4, 10.5, 10.55, 10.6]
    const first = buildLinearBaseline(prices, 3)
    const replay = buildLinearBaseline(prices, 3)
    expect(first).toEqual(replay)
    expect(first?.modelId).toBe('linear_ridge_baseline')
    expect(first?.points).toHaveLength(3)
    expect(first?.points[0]?.narrowEnvelope[0]).toBeLessThan(first?.points[0]?.median ?? 0)
    expect(first?.points[0]).not.toHaveProperty('probability')
  })

  it('fails closed with too little or invalid history', () => {
    expect(buildLinearBaseline([1, 2, 3], 2)).toBeNull()
    expect(buildLinearBaseline([1, 2, 3, 4, 5, 6, 7, Number.NaN], 2)).toBeNull()
  })
})
