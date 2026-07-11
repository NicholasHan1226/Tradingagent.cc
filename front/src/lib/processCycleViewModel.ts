import { marketLabels } from '../data/dashboard'
import type { FunnelEvent } from '../types/dashboard'
import { translateTerminalValue } from './runtimeHeartbeat'

const STAGES: FunnelEvent['stage'][] = ['发现', '研判', '风控', '待确认', '结果']

export type ProcessCycleStage = { label: FunnelEvent['stage']; state: 'complete' | 'current' | 'missing' }
export type ProcessCycleRow = {
  id: string
  symbol: string
  market: string
  result: string
  source: string
  latency: string
  evidence: string
  reason: string
  updatedAt: string
  stages: ProcessCycleStage[]
}

export function createProcessCycles(events: FunnelEvent[]): ProcessCycleRow[] {
  const groups = new Map<string, FunnelEvent[]>()
  for (const event of events) {
    const key = event.opportunityId?.trim() || `${event.market}:${event.symbol}`
    groups.set(key, [...(groups.get(key) ?? []), event])
  }

  return [...groups.entries()]
    .map(([id, grouped]) => toCycle(id, grouped))
    .sort((left, right) => right.sortTime - left.sortTime)
    .map(({ sortTime: _sortTime, ...row }) => row)
}

function toCycle(id: string, events: FunnelEvent[]): ProcessCycleRow & { sortTime: number } {
  const ordered = [...events].sort((left, right) => eventTime(left) - eventTime(right) || (left.sequence ?? 0) - (right.sequence ?? 0))
  const latest = ordered[ordered.length - 1]
  const presentStages = new Set(ordered.map((event) => event.stage))
  const currentStage = latest.stage
  const startedAt = eventTime(ordered[0])
  const updatedAt = eventTime(latest)
  const elapsed = startedAt > 0 && updatedAt > startedAt
    ? Math.round((updatedAt - startedAt) / 60_000)
    : latest.latencyMinutes
  const latestReason = [...ordered].reverse().find((event) => event.reason?.trim())?.reason?.trim()

  return {
    id,
    symbol: latest.symbol,
    market: marketLabels[latest.market],
    result: latest.label || latest.status,
    source: translateTerminalValue(latest.source),
    latency: typeof elapsed === 'number' ? `${elapsed}分钟` : '—',
    evidence: `${presentStages.size}/5 阶段`,
    reason: latestReason || '—',
    updatedAt: formatTimestamp(latest.at),
    stages: STAGES.map((label) => ({ label, state: !presentStages.has(label) ? 'missing' : label === currentStage ? 'current' : 'complete' })),
    sortTime: updatedAt,
  }
}

function eventTime(event: FunnelEvent) {
  if (!event.at) return 0
  const time = new Date(event.at).getTime()
  return Number.isFinite(time) ? time : 0
}

function formatTimestamp(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'Asia/Shanghai',
  }).format(date)
}
