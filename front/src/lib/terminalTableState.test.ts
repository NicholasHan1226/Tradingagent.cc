import { describe, expect, it } from 'vitest'
import { filterAndSortRows } from './terminalTableState'

const rows = [
  { symbol: '600519.SH', reason: '研究通过', score: 82 },
  { symbol: 'BTC-USD', reason: '风险等待', score: 58 },
]

describe('terminal table state', () => {
  it('filters across searchable evidence and sorts selected values', () => {
    expect(filterAndSortRows(rows, 'btc', (row) => `${row.symbol} ${row.reason}`, (row) => row.score, 'asc')[0].symbol).toBe('BTC-USD')
    expect(filterAndSortRows(rows, '', (row) => row.symbol, (row) => row.score, 'desc').map((row) => row.symbol)).toEqual(['600519.SH', 'BTC-USD'])
  })

  it('keeps source order when no sort accessor is selected', () => {
    expect(filterAndSortRows(rows, '', (row) => row.symbol, undefined, 'asc')).toEqual(rows)
  })
})
