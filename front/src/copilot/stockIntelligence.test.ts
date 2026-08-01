import { describe, expect, it } from 'vitest'
import { summarizeStockSentiment, type StockEvent } from './stockIntelligence'

function event(id: string, sentiment: StockEvent['sentiment'], publishedAt: string): StockEvent {
  return {
    id,
    kind: 'sentiment',
    title: id,
    summary: id,
    source: 'test',
    publishedAt,
    sentiment,
    relatedSymbols: ['000400.SZ'],
    url: null,
  }
}

describe('stock sentiment summary', () => {
  it('summarizes only the supplied stock-bound events without inventing probabilities', () => {
    expect(summarizeStockSentiment([
      event('positive-1', 'positive', '2026-08-01T01:00:00.000Z'),
      event('positive-2', 'positive', '2026-08-01T03:00:00.000Z'),
      event('neutral-1', 'neutral', '2026-08-01T02:00:00.000Z'),
    ])).toEqual({
      total: 3,
      positive: 2,
      neutral: 1,
      negative: 0,
      tone: '偏积极',
      latestPublishedAt: '2026-08-01T03:00:00.000Z',
    })
  })

  it('fails visibly empty when no linked event exists', () => {
    expect(summarizeStockSentiment([])).toEqual({
      total: 0,
      positive: 0,
      neutral: 0,
      negative: 0,
      tone: '暂无舆论',
      latestPublishedAt: null,
    })
  })
})
