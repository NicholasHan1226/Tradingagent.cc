import { describe, expect, it } from 'vitest'
import { createAutomationObservatoryViewModel, normalizePage } from './automationObservatoryViewModel'
import type { SignalRow } from '../types/dashboard'
import type { WorkbenchViewModel } from './workbenchViewModel'

const pending: SignalRow = {
  symbol: 'IF2601.CFFEX',
  name: '沪深300期指',
  market: 'CNFutures',
  method: '模拟开盘观察',
  strategyName: '期货自动观察',
  status: 'pending',
  impact: '--',
  confidence: '64%',
  age: '31分钟',
  reason: '等待 5 分钟样本和模拟回执',
  next: '只做模拟观察',
  steps: 5,
  stage: '待执行',
  stageEvidence: 'partial',
}

const executed: SignalRow = {
  ...pending,
  symbol: '600519.SH',
  name: '贵州茅台',
  market: 'A-share',
  status: 'executed',
  reason: '自动成交并写回结果',
  stage: '成交',
  stageEvidence: 'full',
}

const blocked: SignalRow = {
  ...pending,
  symbol: 'BTC-USD',
  name: '比特币',
  market: 'Crypto',
  status: 'blocked',
  reason: '波动超过风险边界',
  stage: '风控',
}

function workbench({
  active = [],
  completed = [],
  review = [],
}: {
  active?: SignalRow[]
  completed?: SignalRow[]
  review?: SignalRow[]
} = {}): WorkbenchViewModel {
  return {
    accountMode: 'simulated',
    market: 'All Markets',
    portfolio: null,
    performance: [],
    headline: {
      pnlAmount: null,
      returnPct: 0,
      targetPct: 0,
      targetGapPct: 0,
      maxDrawdownPct: null,
      capitalBase: null,
      generatedAt: null,
    },
    opportunities: { active, completed },
    positions: [],
    funnelEvents: [],
    reviewItems: review,
    liveGate: { gated: false, title: '模拟盘运行中', detail: '只读模拟结果' },
  }
}

describe('createAutomationObservatoryViewModel', () => {
  it('prioritizes running automation over terminal review rows', () => {
    const model = createAutomationObservatoryViewModel(workbench({
      active: [pending],
      completed: [executed],
      review: [blocked],
    }))

    expect(model.runtimeItem.kind).toBe('running')
    expect(model.runtimeItem.name).toBe('沪深300期指')
    expect(model.running).toEqual([pending])
    expect(model.completed).toEqual([executed])
    expect(model.automaticReview).toEqual([blocked])
    expect(model.summary).toEqual({
      runningCount: 1,
      positionCount: 0,
      completedCount: 1,
      automaticReviewCount: 1,
    })
  })

  it('reports an idle automation state without asking the user to act', () => {
    const model = createAutomationObservatoryViewModel(workbench())

    expect(model.runtimeItem).toEqual(expect.objectContaining({
      kind: 'idle',
      name: '当前没有运行中的自动任务',
      stage: '运行空闲',
      statusLabel: '等待下一轮调度',
    }))
    expect(JSON.stringify(model)).not.toMatch(/下一步|还差什么|待处理|需要复盘/)
  })

  it.each([
    ['主页', '总览'],
    ['机会', '过程'],
    ['决策', '过程'],
    ['收益', '收益'],
  ] as const)('normalizes page %s to %s', (page, expected) => {
    expect(normalizePage(page)).toBe(expected)
  })
})
