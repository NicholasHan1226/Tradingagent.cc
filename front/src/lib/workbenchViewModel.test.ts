import { describe, expect, it } from 'vitest'
import { createWorkbenchViewModel, formatRuntimeReason } from './workbenchViewModel'
import type { MarketSummary, PortfolioSummary, SignalRow } from '../types/dashboard'

const portfolio: PortfolioSummary = {
  pnlAmount: -65,
  returnPct: -0.03,
  capitalBase: 200000,
  targetPct: 8,
  maxDrawdownPct: 0,
  tradeCount: 5,
  pointCount: 3,
  source: 'account',
  pnlCurrency: 'CNY',
  updatedAt: '2026-07-11T09:00:00+08:00',
}

const summaries: MarketSummary[] = [{
  market: 'A-share',
  status: 'ready',
  runtimeState: 'normal',
  holdingCount: 3,
  signalCount: 4,
  tradeCount: 3,
  styleCount: 1,
  capitalBase: 200000,
  pnlAmount: 6931,
  returnPct: 3.47,
  maxDrawdownPct: 0,
  source: 'market-summary',
  headline: 'A股',
  detail: 'A股结果',
  pnlCurrency: 'CNY',
}]

const signals: SignalRow[] = [
  { symbol: '000001.SZ', name: '平安银行', market: 'A-share', method: 'buy', status: 'pending', impact: '--', confidence: '70%', age: '1小时', reason: '等待确认', next: '继续观察', steps: 4 },
  { symbol: '000002.SZ', name: '万科A', market: 'A-share', method: 'buy', status: 'blocked', impact: '--', confidence: '60%', age: '2小时', reason: '风险偏高', next: '等待风险下降', steps: 4 },
  { symbol: '000003.SZ', name: '国农科技', market: 'A-share', method: 'buy', status: 'executed', impact: '--', confidence: '80%', age: '3小时', reason: '已成交', next: '进入复盘', steps: 6 },
  { symbol: '000004.SZ', name: '国华网安', market: 'A-share', method: 'buy', status: 'missed', impact: '--', confidence: '50%', age: '4小时', reason: '已错过', next: '进入复盘', steps: 5 },
]

describe('createWorkbenchViewModel', () => {
  it('forces the chart latest point to equal the selected headline return', () => {
    const view = createWorkbenchViewModel({
      accountMode: 'simulated',
      activeMarket: 'All Markets',
      performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
      portfolio,
      marketSummaries: summaries,
      signals,
      holdings: [],
      funnelEvents: [],
      generatedAt: '2026-07-11T09:00:00+08:00',
    })

    expect(view.performance.at(-1)?.simulated).toBe(view.headline.returnPct)
    expect(view.performance.at(-1)?.target).toBe(view.headline.targetPct)
    expect(view.headline.returnPct).toBe(3.47)
  })

  it('separates active opportunities from terminal outcomes', () => {
    const view = createWorkbenchViewModel({
      accountMode: 'simulated',
      activeMarket: 'All Markets',
      performance: [],
      portfolio,
      marketSummaries: [],
      signals,
      holdings: [],
      funnelEvents: [],
      generatedAt: null,
    })

    expect(view.opportunities.active.map((row) => row.status)).toEqual(['pending', 'blocked'])
    expect(view.opportunities.completed.map((row) => row.status)).toEqual(['executed', 'missed'])
  })

  it('returns a dedicated live gate without changing the selected market', () => {
    const view = createWorkbenchViewModel({
      accountMode: 'live',
      activeMarket: 'A-share',
      performance: [],
      portfolio,
      marketSummaries: summaries,
      signals,
      holdings: [],
      funnelEvents: [],
      generatedAt: null,
    })

    expect(view.market).toBe('A-share')
    expect(view.liveGate).toEqual(expect.objectContaining({ gated: true, title: '实盘待接入' }))
  })
})

describe('formatRuntimeReason', () => {
  it.each([
    ['market_data_missing', '等待行情数据'],
    ['futures_market_data_not_ready', '期货行情尚未就绪'],
    ['crypto_waiting_for_market_data', '加密市场等待行情'],
  ])('maps %s to user copy', (input, expected) => {
    expect(formatRuntimeReason(input)).toBe(expected)
  })
})
