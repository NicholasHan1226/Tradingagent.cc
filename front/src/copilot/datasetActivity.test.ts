import { describe, expect, it } from 'vitest'
import { resolveDatasetActivity } from './datasetActivity'

describe('dataset activity', () => {
  it('keeps a dataset without its own clock authority as a coverage gap', () => {
    expect(resolveDatasetActivity({
      datasetId: 'cn.dataset.rt_min',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: null,
    })).toMatchObject({
      datasetId: 'cn.dataset.rt_min',
      state: 'coverage_gap',
      reason: 'dataset_activity_authority_missing',
    })
  })

  it('resolves each dataset from its own authority instead of a shared market clock', () => {
    const ashare = resolveDatasetActivity({
      datasetId: 'cn.dataset.rt_min',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { market: 'CN-A', timezone: 'Asia/Shanghai', calendar: 'sse_trade_calendar', session: 'open' },
    })
    const crypto = resolveDatasetActivity({
      datasetId: 'crypto.binance.spot_5m',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { market: 'CRYPTO', timezone: 'UTC', calendar: 'continuous', session: 'closed' },
    })
    const futures = resolveDatasetActivity({
      datasetId: 'cn.futures.m5',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { market: 'CN-FUTURES', timezone: 'Asia/Shanghai', calendar: 'cffex_trade_date', session: 'closed' },
    })

    expect(ashare.state).toBe('live')
    expect(crypto.state).toBe('closed')
    expect(futures.state).toBe('closed')
    expect(new Set([ashare.clockKey, crypto.clockKey, futures.clockKey]).size).toBe(3)
  })

  it('fails closed when any part of an authority is unknown', () => {
    expect(resolveDatasetActivity({
      datasetId: 'cn.dataset.anns_d',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { market: 'CN-A', timezone: 'Asia/Shanghai', calendar: 'sse_trade_calendar', session: 'unknown' },
    })).toMatchObject({ state: 'coverage_gap', reason: 'dataset_activity_authority_incomplete' })
  })
})
