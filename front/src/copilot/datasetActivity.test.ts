import { describe, expect, it } from 'vitest'
import { resolveDatasetActivity, type DatasetActivityAuthority } from './datasetActivity'

const authority = (state: 'open' | 'closed' | 'halted') => ({
  datasetId: 'cn.dataset.rt_min', market: 'ashare' as const, timezone: 'Asia/Shanghai' as const,
  calendar: { id: 'sse', version: 'v1', sourceDatasetId: 'cn.market.trade_calendar' as const, receiptId: 'calendar-receipt', receiptSha256: 'a'.repeat(64), lineageSha256: 'b'.repeat(64), calendarSha256: 'c'.repeat(64) },
  session: { state, asOf: '2026-08-03T01:00:10.000Z' }, dataThrough: '2026-08-03T01:00:00.000Z',
  source: { receiptId: 'source-receipt', receiptSha256: 'd'.repeat(64), lineageSha256: 'e'.repeat(64) },
})

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
      quality: 'unavailable',
    })
  })

  it('keeps receipt-bound factual data usable when session authority is absent', () => {
    expect(resolveDatasetActivity({
      datasetId: 'cn.dataset.daily',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: null,
      receiptBound: true,
    })).toMatchObject({
      state: 'coverage_gap',
      quality: 'usable_degraded',
      missingFields: ['calendar.id', 'calendar.version', 'calendar.receiptId', 'calendar.receiptSha256', 'calendar.lineageSha256', 'calendar.calendarSha256', 'session.state', 'session.asOf'],
    })
  })

  it('resolves each dataset from its own authority instead of a shared market clock', () => {
    const ashare = resolveDatasetActivity({
      datasetId: 'cn.dataset.rt_min',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: authority('open'),
    })
    const daily = resolveDatasetActivity({
      datasetId: 'cn.dataset.daily',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { ...authority('closed'), datasetId: 'cn.dataset.daily', calendar: { ...authority('closed').calendar, id: 'szse' } },
    })
    const announcements = resolveDatasetActivity({
      datasetId: 'cn.dataset.anns_d',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: { ...authority('closed'), datasetId: 'cn.dataset.anns_d', calendar: { ...authority('closed').calendar, id: 'sse-announcement-calendar' } },
    })

    expect(ashare.state).toBe('live')
    expect(daily.state).toBe('closed')
    expect(announcements.state).toBe('closed')
    expect(new Set([ashare.clockKey, daily.clockKey, announcements.clockKey]).size).toBe(3)
  })

  it('fails closed when any part of an authority is unknown', () => {
    expect(resolveDatasetActivity({
      datasetId: 'cn.dataset.anns_d',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: {
        ...authority('open'),
        session: { state: 'unknown', asOf: '2026-08-03T01:00:10.000Z' },
      } as unknown as DatasetActivityAuthority,
    })).toMatchObject({ state: 'coverage_gap', reason: 'dataset_activity_authority_incomplete' })
  })

  it('fails closed when an authority does not bind this dataset and dataThrough', () => {
    expect(resolveDatasetActivity({
      datasetId: 'cn.dataset.daily',
      dataThrough: '2026-08-03T01:00:00.000Z',
      freshness: 'fresh',
      authority: authority('open'),
    })).toMatchObject({ state: 'coverage_gap', reason: 'dataset_activity_authority_incomplete' })
  })
})
