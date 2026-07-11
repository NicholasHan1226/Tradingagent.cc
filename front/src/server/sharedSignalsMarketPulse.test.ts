import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { HoldingRow, SignalRow } from '../types/dashboard'
import { readSharedSignalsMarketPulses, resetMarketPulseCacheForTests } from './sharedSignalsMarketPulse'

const holding = (market: HoldingRow['market'], symbol: string): HoldingRow => ({
  market, symbol, name: symbol, weight: '0%', pnl: '—', risk: '正常', role: '观察',
})

const signal = (market: SignalRow['market'], symbol: string): SignalRow => ({
  market, symbol, name: symbol, method: 'test', status: 'pending', impact: '—', confidence: '—', age: '—', reason: '—', next: '—', steps: 1,
})

describe('SharedSignals market pulse reader', () => {
  beforeEach(() => resetMarketPulseCacheForTests())

  it('routes representative symbols to bounded read-only endpoints and normalizes sourced bars', async () => {
    const fetchImpl = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const rows = url.includes('market=Futures')
        ? [
            { symbol: 'RB2609.SHF', close: 3510, high: 3520, low: 3490, volume: 900, bar_time: '2026-07-11T09:30:00+08:00' },
            { symbol: 'RB2609.SHF', close: 3520, high: 3530, low: 3500, volume: 1000, bar_time: '2026-07-11T09:35:00+08:00' },
          ]
        : [
            { symbol: '600519.SH', close: 1410, high: 1420, low: 1400, volume: 800, bar_time: '2026-07-11T09:30:00+08:00' },
            { symbol: '600519.SH', close: 1424.1, high: 1430, low: 1410, volume: 1200, bar_time: '2026-07-11T09:35:00+08:00' },
          ]
      return new Response(JSON.stringify({ data: rows, metadata: { degraded: false }, source: 'sqlite:market_bars_intraday' }), { status: 200 })
    })

    const pulses = await readSharedSignalsMarketPulses({
      baseUrl: 'http://127.0.0.1:8082',
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      now: new Date('2026-07-11T09:40:00+08:00'),
      signals: [signal('CNFutures', 'RB2609.SHF')],
    })

    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(String(fetchImpl.mock.calls[0][0])).toContain('/realtime_5min?market=Ashare&ts_code=600519.SH&limit=24')
    expect(String(fetchImpl.mock.calls[1][0])).toContain('/realtime_5min?market=Futures&ts_code=RB2609.SHF&limit=24')
    expect(pulses[0]).toMatchObject({ market: 'A-share', symbol: '600519.SH', lastPrice: 1424.1, changePct: 1, high: 1430, low: 1400, volume: 2000, freshness: 'live' })
    expect(pulses[0].points).toEqual([1410, 1424.1])
    expect(pulses[0].source).toContain('market_bars_intraday')
  })

  it('reuses the short cache and degrades without inventing rows', async () => {
    const fetchImpl = vi.fn(async () => { throw new Error('unavailable') })
    const input = { baseUrl: 'http://127.0.0.1:8082', fetchImpl, holdings: [holding('A-share', '000001.SZ')], signals: [] }

    expect(await readSharedSignalsMarketPulses(input)).toEqual([])
    expect(await readSharedSignalsMarketPulses(input)).toEqual([])
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('orders newest-first API rows chronologically before deriving the pulse', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      data: [
        { close: 1424.1, bar_time: '2026-07-11T09:35:00+08:00' },
        { close: 1410, bar_time: '2026-07-11T09:30:00+08:00' },
      ],
      metadata: { degraded: false },
    }), { status: 200 }))

    const [pulse] = await readSharedSignalsMarketPulses({
      baseUrl: 'http://127.0.0.1:8082',
      fetchImpl,
      holdings: [holding('A-share', '600519.SH')],
      signals: [],
      now: new Date('2026-07-11T09:40:00+08:00'),
    })

    expect(pulse.points).toEqual([1410, 1424.1])
    expect(pulse.lastPrice).toBe(1424.1)
  })

  it('treats compact daily trade dates as valid freshness evidence', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      data: [{ close: 288.39, trade_date: '20260711' }],
      metadata: { degraded: false },
    }), { status: 200 }))

    const [pulse] = await readSharedSignalsMarketPulses({
      baseUrl: 'http://127.0.0.1:8082',
      fetchImpl,
      holdings: [holding('US', 'AAPL')],
      signals: [],
      now: new Date('2026-07-12T09:00:00+08:00'),
    })

    expect(pulse.updatedAt).toBe('20260711')
    expect(pulse.freshness).toBe('live')
  })

  it('uses only the canonical yes outcome for Polymarket history', async () => {
    const pmRow = (price: number, priceTime: string, outcome: 'Yes' | 'No') => ({
      price,
      price_time: priceTime,
      raw_json: JSON.stringify({ outcome }),
    })
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      data: [
        pmRow(0.8, '2026-07-11T05:00:00+00:00', 'No'),
        pmRow(0.2, '2026-07-11T05:00:00+00:00', 'Yes'),
        pmRow(0.75, '2026-07-11T04:30:00+00:00', 'No'),
        pmRow(0.25, '2026-07-11T04:30:00+00:00', 'Yes'),
      ],
      metadata: { degraded: false },
    }), { status: 200 }))

    const [pulse] = await readSharedSignalsMarketPulses({
      baseUrl: 'http://127.0.0.1:8082',
      fetchImpl,
      holdings: [],
      signals: [signal('PM', '561263')],
      now: new Date('2026-07-11T05:05:00+00:00'),
    })

    expect(pulse.points).toEqual([0.25, 0.2])
    expect(pulse.lastPrice).toBe(0.2)
    expect(pulse.updatedAt).toBe('2026-07-11T05:00:00+00:00')
  })
})
