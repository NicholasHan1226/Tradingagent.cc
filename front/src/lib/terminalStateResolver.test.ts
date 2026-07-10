import { describe, expect, it } from 'vitest'
import type { HoldingRow, SignalRow } from '../types/dashboard'
import { resolveTerminalState, selectAvailableTab } from './terminalStateResolver'

const base: SignalRow = {
  symbol: 'IF2601.CFFEX', name: '沪深300期指', market: 'CNFutures', method: '模拟观察',
  status: 'pending', impact: '—', confidence: '64%', age: '31分钟', reason: '等待样本', next: '自动等待', steps: 5,
}
const blocked: SignalRow = { ...base, symbol: '000776.SZ', name: '广发证券', market: 'A-share', status: 'blocked', stage: '拒绝', reason: '证据有限' }
const executed: SignalRow = { ...base, symbol: '600030.SH', name: '中信证券', market: 'A-share', status: 'executed', stage: '成交', reason: '结果已写回' }
const position: HoldingRow = { symbol: '600030.SH', name: '中信证券', market: 'A-share', weight: '¥10,000', pnl: '+¥120', risk: '正常', role: '模拟盘持仓' }

describe('resolveTerminalState', () => {
  it('never treats blocked review rows as running automation', () => {
    const state = resolveTerminalState({ signals: [blocked, executed], positions: [] })

    expect(state.running).toEqual([])
    expect(state.completed).toEqual([executed])
    expect(state.review).toEqual([blocked])
    expect(state.preferredTab).toBe('completed')
    expect(state.runtimeItem).toEqual(expect.objectContaining({ kind: 'blocked', contextLabel: '最近事件' }))
  })

  it('prioritizes a real pending process everywhere', () => {
    const state = resolveTerminalState({ signals: [blocked, executed, base], positions: [position] })

    expect(state.preferredTab).toBe('active')
    expect(state.counts).toEqual({ running: 1, completed: 1, positions: 1, review: 1 })
    expect(state.runtimeItem).toEqual(expect.objectContaining({ kind: 'running', contextLabel: '当前运行' }))
  })

  it('moves off an empty tab but preserves a useful explicit selection', () => {
    const state = resolveTerminalState({ signals: [blocked, executed], positions: [position] })

    expect(selectAvailableTab('active', state)).toBe('completed')
    expect(selectAvailableTab('positions', state)).toBe('positions')
    expect(selectAvailableTab('review', state)).toBe('review')
  })
})
