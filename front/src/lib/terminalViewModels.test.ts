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
  { symbol: '300759.SZ', name: '300759.SZ', market: 'A-share', weight: '¥64,486', pnl: '+¥6,826', risk: '正常', role: '模拟盘持仓', quantity: 100, averagePrice: 580.2, markPrice: 644.86, costBasis: 58020, marketValue: 64486, currency: 'CNY', accountScope: 'ashare-main', source: 'position_snapshot' },
  { symbol: '600030.SH', name: '中信证券', market: 'A-share', weight: '¥59,640', pnl: '+¥762', risk: '正常', role: '模拟盘持仓', accountScope: 'ashare-main' },
  { symbol: '000776.SZ', name: '000776.SZ', market: 'A-share', weight: '¥28,275', pnl: '-¥657', risk: '观察', role: '模拟盘持仓', accountScope: 'ashare-main' },
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
    const mixed = [...holdings.slice(0, 1), {
      ...holdings[1],
      market: 'Crypto' as const,
      weight: '8,000 USDT',
      currency: 'USDT' as const,
      marketValue: 8_000,
      averagePrice: 4_000,
      costBasis: 7_500,
      dayPnl: 25,
    }]

    expect(summarizePortfolioCurrency(mixed)).toEqual({ currency: 'mixed', label: '多账户不可汇总' })
    const rows = createPortfolioLedgerRows(mixed)
    expect(rows[0].weight).toBe('100.0%')
    expect(rows[1]).toEqual(expect.objectContaining({
      weight: '100.0%',
      marketValue: '8,000 USDT',
      averagePrice: '4,000.00 USDT',
      costBasis: '7,500 USDT',
      dayPnl: '+25 USDT',
    }))
    expect(rows[1].marketValue).not.toContain('$')
  })

  it('summarizes a single Crypto book as native USDT', () => {
    const crypto: HoldingRow[] = [{
      ...holdings[0],
      symbol: 'BTC-USDT',
      market: 'Crypto',
      weight: '1,169.78 USDT',
      pnl: '+12.5 USDT',
      currency: 'USDT',
      marketValue: 1_169.78,
    }]

    expect(summarizePortfolioCurrency(crypto)).toEqual({ currency: 'USDT', label: '1,170 USDT' })
  })

  it('fails closed for a retired USD holding input', () => {
    const retiredUsd = [{ ...holdings[0], currency: 'USD' as const, weight: '$100', marketValue: 100 }]

    expect(summarizePortfolioCurrency(retiredUsd)).toEqual({ currency: 'mixed', label: '币种不可用' })
    expect(createPortfolioLedgerRows(retiredUsd)[0].marketValue).toBe('—')
  })

  it('does not combine independent A-share and CNFutures CNY authorities', () => {
    const independent: HoldingRow[] = [
      { ...holdings[0], marketValue: 100, weight: '¥100', pnl: '+¥10', currency: 'CNY' },
      { ...holdings[1], symbol: 'IF2601.CFFEX', market: 'CNFutures', marketValue: 300, weight: '¥300', pnl: '+¥30', currency: 'CNY' },
    ]

    const rows = createPortfolioLedgerRows(independent)
    expect(rows.map((row) => row.weight)).toEqual(['100.0%', '100.0%'])
    expect(rows.map((row) => row.contribution)).toEqual(['+100.0%', '+100.0%'])
    expect(summarizePortfolioCurrency(independent)).toEqual({ currency: 'mixed', label: '多账户不可汇总' })
    expect(summarizePortfolioCurrency(independent).label).not.toContain('400')
  })

  it('uses account scope in Crypto holding denominators and refuses account totals', () => {
    const independent: HoldingRow[] = [
      { ...holdings[0], symbol: 'BTC-USDT', market: 'Crypto', currency: 'USDT', accountScope: 'crypto:grid', marketValue: 1_200, weight: '1,200 USDT', pnl: '+20 USDT' },
      { ...holdings[1], symbol: 'ETH-USDT', market: 'Crypto', currency: 'USDT', accountScope: 'crypto:momentum', marketValue: 800, weight: '800 USDT', pnl: '+10 USDT' },
    ]

    const rows = createPortfolioLedgerRows(independent)
    expect(rows.map((row) => row.weight)).toEqual(['100.0%', '100.0%'])
    expect(rows.map((row) => row.contribution)).toEqual(['+100.0%', '+100.0%'])
    expect(summarizePortfolioCurrency(independent)).toEqual({ currency: 'mixed', label: '多账户不可汇总' })

    const missingScope = independent.map(({ accountScope: _accountScope, ...holding }) => holding)
    expect(createPortfolioLedgerRows(missingScope).map((row) => row.weight)).toEqual(['—', '—'])
    expect(summarizePortfolioCurrency(missingScope)).toEqual({ currency: 'mixed', label: '账户范围不可用' })
  })

  it('creates a risk ledger from reviewable terminal states', () => {
    const rows = createRiskLedgerRows([
      completed,
      { ...completed, symbol: 'BTC-USDT', status: 'blocked', stage: '风控', reason: '超过风险边界', stageEvidence: 'partial' },
      { ...completed, symbol: 'IF2601.CFFEX', status: 'missed', stage: '错过', reason: '窗口已经结束' },
    ])

    expect(rows.map((row) => row.symbol)).toEqual(['BTC-USDT', 'IF2601.CFFEX'])
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
