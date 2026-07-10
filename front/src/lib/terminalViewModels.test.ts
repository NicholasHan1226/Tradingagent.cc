import { describe, expect, it } from 'vitest'
import type { HoldingRow, SignalRow } from '../types/dashboard'
import {
  createPortfolioLedgerRows,
  createProcessBookRows,
  createRiskLedgerRows,
  summarizePortfolioCurrency,
} from './terminalViewModels'

const completed: SignalRow = {
  symbol: '600030.SH',
  name: '中信证券',
  market: 'A-share',
  method: '趋势观察',
  status: 'executed',
  impact: '+1.2',
  confidence: '82%',
  age: '12分钟',
  reason: '模拟成交完成',
  next: '保持风控参数',
  steps: 6,
  stage: '成交',
  stageEvidence: 'full',
  stageLatencyMinutes: 8,
}

const holdings: HoldingRow[] = [
  { symbol: '300759.SZ', name: '300759.SZ', market: 'A-share', weight: '¥64,486', pnl: '+¥6,826', risk: '正常', role: '模拟盘持仓', quantity: 100, averagePrice: 580.2, markPrice: 644.86, costBasis: 58020, marketValue: 64486, currency: 'CNY', source: 'position_snapshot' },
  { symbol: '600030.SH', name: '中信证券', market: 'A-share', weight: '¥59,640', pnl: '+¥762', risk: '正常', role: '模拟盘持仓' },
  { symbol: '000776.SZ', name: '000776.SZ', market: 'A-share', weight: '¥28,275', pnl: '-¥657', risk: '观察', role: '模拟盘持仓' },
]

describe('terminal view models', () => {
  it('falls back to completed process rows when nothing is running', () => {
    const model = createProcessBookRows([], [completed])

    expect(model.mode).toBe('completed')
    expect(model.title).toBe('最近完成')
    expect(model.rows[0]).toEqual(expect.objectContaining({
      symbol: '600030.SH',
      stage: '结果写回',
      evidence: '证据完整',
      latency: '8分钟',
      result: '结果已写回',
    }))
  })

  it('formats an A-share amount portfolio as CNY and derives weights', () => {
    const model = createPortfolioLedgerRows(holdings)

    expect(summarizePortfolioCurrency(holdings)).toEqual({ currency: 'CNY', label: '¥152,401' })
    expect(model[0].assetName).toBe('')
    expect(model[0].weight).toBe('42.3%')
    expect(model[0].marketValue).toBe('¥64,486')
    expect(model[0]).toEqual(expect.objectContaining({ quantity: '100', averagePrice: '¥580.20', markPrice: '¥644.86', costBasis: '¥58,020', source: '持仓快照' }))
    expect(model[2].risk).toBe('观察')
  })

  it('does not invent a mixed-currency aggregate', () => {
    const mixed = [...holdings.slice(0, 1), { ...holdings[1], market: 'US' as const, weight: '$8,000' }]

    expect(summarizePortfolioCurrency(mixed)).toEqual({ currency: 'mixed', label: '多币种' })
  })

  it('creates a risk ledger from reviewable terminal states', () => {
    const rows = createRiskLedgerRows([
      completed,
      { ...completed, symbol: 'BTC-USD', status: 'blocked', stage: '风控', reason: '超过风险边界', stageEvidence: 'partial' },
      { ...completed, symbol: 'IF2601.CFFEX', status: 'missed', stage: '错过', reason: '窗口已经结束' },
    ])

    expect(rows.map((row) => row.symbol)).toEqual(['BTC-USD', 'IF2601.CFFEX'])
    expect(rows[0]).toEqual(expect.objectContaining({ gate: '安全拦截', evidence: '证据有限' }))
  })

  it('adds stale data domains to the risk ledger', () => {
    const rows = createRiskLedgerRows([], { signals: 'stale', holdings: 'ready', risk: 'error' })

    expect(rows).toEqual(expect.arrayContaining([
      expect.objectContaining({ symbol: 'DATA/SIGNALS', gate: '快照滞后', evidence: '证据有限' }),
      expect.objectContaining({ symbol: 'DATA/RISK', gate: '读取异常', evidence: '证据不可用' }),
    ]))
  })
})
