import { describe, expect, it } from 'vitest'
import type { PortfolioSummary, SignalRow } from '../types/dashboard'
import { getHoldingsEmptyEvidence, getPerformanceDensity } from './terminalDensity'

const flatPortfolio: PortfolioSummary = {
  pnlAmount: 0, returnPct: 0, capitalBase: 50000, targetPct: 8, maxDrawdownPct: 0,
  tradeCount: 0, pointCount: 1, source: 'local_sim', updatedAt: '2026-07-11T05:20:00.000Z',
  pnlCurrency: 'CNY', ashareAccount: {
    capitalAuthorityId: 'ashare-capital-v1', authorityGeneration: 1,
    executionLineageId: 'ashare-sim-fresh-20260712-v1',
    cashAvailable: 50000, marketValue: 0, accountEquity: 50000, accountTotalPnl: 0,
    accountReturnPct: 0, openPositionCount: 0, totalSampleCount: 0, validationSampleCount: 0,
    strategySampleValidCount: 0, source: 'local_sim', updatedAt: '2026-07-11T05:20:00.000Z',
  },
}
const closed: SignalRow = {
  symbol: '600519.SH', name: '贵州茅台', market: 'A-share', method: 'buy', status: 'executed',
  impact: '+1.2%', confidence: '80%', age: '2小时前', reason: '模拟结果已写回', next: '自动复盘', steps: 6,
}

describe('terminal density', () => {
  it('distinguishes active movement from quiet and absent performance', () => {
    expect(getPerformanceDensity([], null)).toBe('empty')
    expect(getPerformanceDensity([{ day: '现在', simulated: 0, target: 8, benchmark: 0, opportunity: 0 }], flatPortfolio)).toBe('quiet')
    expect(getPerformanceDensity([
      { day: '上午', simulated: 0, target: 8, benchmark: 0, opportunity: 0 },
      { day: '现在', simulated: 0.8, target: 8, benchmark: 0.1, opportunity: -0.2 },
    ], flatPortfolio)).toBe('active')
  })

  it('builds an honest holdings empty state from sourced account and closed-process facts', () => {
    expect(getHoldingsEmptyEvidence({ holdings: [], signals: [closed], portfolio: flatPortfolio, generatedAt: '2026-07-11T05:20:00.000Z' })).toEqual({
      title: '当前没有模拟持仓',
      detail: '资金保持未部署；等待通过证据与风险门禁的新机会。',
      rows: [
        ['当前敞口', '0 项'],
        ['可用资金', '¥50,000'],
        ['最近关闭', '600519.SH · 结果已写回'],
        ['数据时间', '07/11 13:20'],
      ],
    })
  })
})
