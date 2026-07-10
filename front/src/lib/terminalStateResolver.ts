import type { HoldingRow, Market, SignalRow } from '../types/dashboard'

export type TerminalTab = 'active' | 'positions' | 'completed' | 'review'
export type AutomationRuntimeKind = 'running' | 'waiting' | 'blocked' | 'completed' | 'idle'

export type AutomationRuntimeItem = {
  kind: AutomationRuntimeKind
  contextLabel: '当前运行' | '最近事件' | '运行空闲'
  symbol: string | null
  name: string
  market: Market | null
  strategy: string
  stage: string
  statusLabel: string
  evidenceLabel: string
  updatedAtLabel: string
  detail: string
}

export type TerminalResolvedState = {
  running: SignalRow[]
  completed: SignalRow[]
  review: SignalRow[]
  positions: HoldingRow[]
  runtimeItem: AutomationRuntimeItem
  preferredTab: TerminalTab
  counts: { running: number; completed: number; positions: number; review: number }
}

export function resolveTerminalState({ signals, positions }: { signals: SignalRow[]; positions: HoldingRow[] }): TerminalResolvedState {
  const unique = dedupeSignals(signals)
  const running = unique.filter((signal) => signal.status === 'pending')
  const completed = unique.filter((signal) => signal.status === 'executed' || signal.queueBucket?.toLowerCase() === 'partial')
  const review = unique.filter((signal) => signal.status === 'blocked' || signal.status === 'missed' || signal.status === 'cancelled')
  const counts = { running: running.length, completed: completed.length, positions: positions.length, review: review.length }
  const preferredTab = preferredTabFromCounts(counts)

  return {
    running,
    completed,
    review,
    positions,
    runtimeItem: createRuntimeItem(running, review, completed),
    preferredTab,
    counts,
  }
}

export function selectAvailableTab(current: TerminalTab, state: Pick<TerminalResolvedState, 'preferredTab' | 'counts'>): TerminalTab {
  const count = current === 'active' ? state.counts.running : state.counts[current]
  return count > 0 ? current : state.preferredTab
}

function preferredTabFromCounts(counts: TerminalResolvedState['counts']): TerminalTab {
  if (counts.running) return 'active'
  if (counts.completed) return 'completed'
  if (counts.positions) return 'positions'
  if (counts.review) return 'review'
  return 'active'
}

function createRuntimeItem(running: SignalRow[], review: SignalRow[], completed: SignalRow[]): AutomationRuntimeItem {
  const active = running[0]
  if (active) return fromSignal(active, 'running', '当前运行')
  const blocked = review.find((signal) => signal.status === 'blocked')
  if (blocked) return fromSignal(blocked, 'blocked', '最近事件')
  const waiting = review[0]
  if (waiting) return fromSignal(waiting, 'waiting', '最近事件')
  const result = completed[0]
  if (result) return fromSignal(result, 'completed', '最近事件')
  return {
    kind: 'idle',
    contextLabel: '运行空闲',
    symbol: null,
    name: '当前没有运行中的自动任务',
    market: null,
    strategy: '自动化系统',
    stage: '运行空闲',
    statusLabel: '等待下一轮调度',
    evidenceLabel: '快照正常',
    updatedAtLabel: '等待新事件',
    detail: '收益、持仓和历史结果继续保留。',
  }
}

function fromSignal(signal: SignalRow, kind: Exclude<AutomationRuntimeKind, 'idle'>, contextLabel: AutomationRuntimeItem['contextLabel']): AutomationRuntimeItem {
  return {
    kind,
    contextLabel,
    symbol: signal.symbol,
    name: signal.name,
    market: signal.market,
    strategy: formatStrategy(signal.strategyName ?? signal.method),
    stage: formatStage(signal, kind),
    statusLabel: formatStatus(signal, kind),
    evidenceLabel: formatEvidence(signal.stageEvidence),
    updatedAtLabel: signal.age,
    detail: signal.reason === '等待下一次确认' ? '等待自动确认' : signal.reason,
  }
}

function dedupeSignals(signals: SignalRow[]) {
  const rows = new Map<string, SignalRow>()
  signals.forEach((signal) => rows.set(`${signal.symbol}|${signal.status}|${signal.queueBucket ?? ''}|${signal.age}`, signal))
  return [...rows.values()]
}

function formatStrategy(value: string) {
  if (value.toLowerCase() === 'buy') return '买入流程'
  if (value.toLowerCase() === 'sell') return '卖出流程'
  return value
}

function formatStage(signal: SignalRow, kind: Exclude<AutomationRuntimeKind, 'idle'>) {
  if (signal.stage === '评分') return '研究'
  if (signal.stage === '待执行') return '模拟执行'
  if (signal.stage === '成交') return '结果写回'
  if (signal.stage) return signal.stage
  if (kind === 'blocked') return '风控'
  if (kind === 'completed') return '结果写回'
  return '自动等待'
}

function formatStatus(signal: SignalRow, kind: Exclude<AutomationRuntimeKind, 'idle'>) {
  if (kind === 'running') return '自动运行中'
  if (kind === 'blocked') return '安全拦截'
  if (signal.queueBucket?.toLowerCase() === 'partial') return '部分成交'
  if (signal.status === 'executed') return '结果已写回'
  if (signal.status === 'cancelled') return '流程已取消'
  if (signal.status === 'missed') return '自动复盘中'
  return '自动等待'
}

function formatEvidence(evidence?: SignalRow['stageEvidence']) {
  if (evidence === 'full') return '证据完整'
  if (evidence === 'replay') return '历史回放'
  if (evidence === 'partial') return '证据有限'
  return '证据待写入'
}
