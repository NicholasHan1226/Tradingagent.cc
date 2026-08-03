import { describe, expect, it, vi } from 'vitest'
import { assertTrackingUniverse, loadTrackingUniverse } from './trackingUniverse'

const universe = {
  contractId: 'tradingagent.trading_copilot_tracking_universe.v1' as const,
  generatedAt: '2026-08-03T01:00:00.000Z',
  items: [{ symbol: '000400.SZ', name: '许继电气' }, { symbol: '601899.SH', name: '紫金矿业' }],
}

describe('TradingCopilot tracking universe contract', () => {
  it('accepts a bounded, named A-share mapping', () => {
    expect(() => assertTrackingUniverse(universe)).not.toThrow()
  })

  it('rejects duplicate symbols and makes the client fail closed', async () => {
    const duplicate = { ...universe, items: [universe.items[0], universe.items[0]] }
    expect(() => assertTrackingUniverse(duplicate)).toThrow('tracking_universe_item_invalid')
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(duplicate), { status: 200 }))
    await expect(loadTrackingUniverse(fetcher)).resolves.toBeNull()
  })

  it('rejects empty and extended payloads rather than silently accepting a new contract shape', () => {
    expect(() => assertTrackingUniverse({ ...universe, items: [] })).toThrow('tracking_universe_invalid')
    expect(() => assertTrackingUniverse({ ...universe, unverified: true })).toThrow('tracking_universe_invalid')
  })
})
