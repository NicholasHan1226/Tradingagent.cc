import type { HoldingRow, LegacyPage, Market, Page, SignalRow } from '../types/dashboard'
import type { WorkbenchViewModel } from './workbenchViewModel'

export type AutomationRuntimeKind = 'running' | 'waiting' | 'blocked' | 'completed' | 'idle'

export type AutomationRuntimeItem = {
  kind: AutomationRuntimeKind
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

export type AutomationObservatoryViewModel = {
  running: SignalRow[]
  positions: HoldingRow[]
  completed: SignalRow[]
  automaticReview: SignalRow[]
  runtimeItem: AutomationRuntimeItem
  summary: {
    runningCount: number
    positionCount: number
    completedCount: number
    automaticReviewCount: number
  }
}

const PAGE_ALIASES: Record<LegacyPage, Page> = {
  主页: '总览',
  机会: '过程',
  决策: '过程',
}

export function normalizePage(page: Page | LegacyPage): Page {
  return page in PAGE_ALIASES ? PAGE_ALIASES[page as LegacyPage] : page as Page
}

export function createAutomationObservatoryViewModel(
  workbench: WorkbenchViewModel,
): AutomationObservatoryViewModel {
  const running = workbench.opportunities.active.filter((signal) => signal.status === 'pending')
  const completed = workbench.opportunities.completed
  const automaticReview = workbench.reviewItems

  return {
    running,
    positions: workbench.positions,
    completed,
    automaticReview,
    runtimeItem: createRuntimeItem(running, automaticReview, completed),
    summary: {
      runningCount: running.length,
      positionCount: workbench.positions.length,
      completedCount: completed.length,
      automaticReviewCount: automaticReview.length,
    },
  }
}

function createRuntimeItem(
  running: SignalRow[],
  automaticReview: SignalRow[],
  completed: SignalRow[],
): AutomationRuntimeItem {
  const active = running[0]
  if (active) return fromSignal(active, 'running')

  const blocked = automaticReview.find((signal) => signal.status === 'blocked')
  if (blocked) return fromSignal(blocked, 'blocked')

  const waiting = automaticReview[0]
  if (waiting) return fromSignal(waiting, 'waiting')

  const result = completed[0]
  if (result) return fromSignal(result, 'completed')

  return {
    kind: 'idle',
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

function fromSignal(signal: SignalRow, kind: Exclude<AutomationRuntimeKind, 'idle'>): AutomationRuntimeItem {
  return {
    kind,
    symbol: signal.symbol,
    name: signal.name,
    market: signal.market,
    strategy: formatStrategy(signal.strategyName ?? signal.method),
    stage: formatStage(signal, kind),
    statusLabel: formatStatus(signal, kind),
    evidenceLabel: formatEvidence(signal.stageEvidence),
    updatedAtLabel: signal.age,
    detail: formatDetail(signal.reason),
  }
}

function formatStrategy(value: string) {
  if (value.toLowerCase() === 'buy') return '买入流程'
  if (value.toLowerCase() === 'sell') return '卖出流程'
  return value
}

function formatDetail(value: string) {
  if (value === '等待下一次确认') return '等待自动确认'
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
