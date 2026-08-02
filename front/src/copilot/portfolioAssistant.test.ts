import { describe, expect, it } from 'vitest'
import { buildPortfolioAssistantReport } from './portfolioAssistant.ts'
import { emptyTradingCopilotState } from './types.ts'

describe('buildPortfolioAssistantReport', () => {
  it('keeps cost, T+1, sector exposure and human-plan risk separate', () => {
    const state = emptyTradingCopilotState('2026-08-02T00:00:00Z')
    state.account = { declaredCapitalCny: 200_000, availableCashCny: 80_000, updatedAt: state.updatedAt }
    state.holdings = [
      { symbol: '000400.SZ', name: '许继电气', quantity: 2_000, sellableQuantity: 1_000, averageCost: 30, updatedAt: state.updatedAt },
      { symbol: '600089.SH', name: '特变电工', quantity: 2_000, sellableQuantity: 0, averageCost: 20, updatedAt: state.updatedAt },
    ]
    state.decisions = [{
      id: 'plan-1', symbol: '000400.SZ', action: 'planned', recordedAt: state.updatedAt, actor: 'nicholas', authority: 'human_intent_only',
      plan: { reason: '等待确认', trigger: '放量', invalidation: '跌破', maxRiskCny: 3_000 }, review: null,
    }]
    const intelligence = {
      '000400.SZ': { company: { industry: '电网设备' } },
      '600089.SH': { company: { industry: '电网设备' } },
    } as never
    const report = buildPortfolioAssistantReport(state, intelligence)
    expect(report.costBasisCny).toBe(100_000)
    expect(report.declaredUtilizationPct).toBe(50)
    expect(report.sellableQuantity).toBe(1_000)
    expect(report.industryExposure[0]).toMatchObject({ industry: '电网设备', costBasisCny: 100_000, sharePct: 50 })
    expect(report.pendingRiskBudgetCny).toBe(3_000)
    expect(report.checks.find((item) => item.id === 'industry_concentration')?.level).toBe('attention')
  })

  it('fails closed when declared capital is absent and never invents sector names', () => {
    const state = emptyTradingCopilotState('2026-08-02T00:00:00Z')
    state.holdings = [{ symbol: '000001.SZ', name: '平安银行', quantity: 100, sellableQuantity: 0, averageCost: 10, updatedAt: state.updatedAt }]
    const report = buildPortfolioAssistantReport(state, {})
    expect(report.declaredUtilizationPct).toBeNull()
    expect(report.industryExposure[0]?.industry).toBe('行业待正式投影补全')
    expect(report.checks.find((item) => item.id === 'account_consistency')?.level).toBe('blocked')
  })
})
