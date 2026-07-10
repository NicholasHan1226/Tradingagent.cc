import { marketLabels } from '../data/dashboard'
import type { FunnelEvent } from '../types/dashboard'

export type ProcessEventRow = {
  id: string
  symbol: string
  market: string
  stage: string
  result: string
  source: string
  latency: string
  reason: string
  timestamp: string
}

const SOURCE_LABELS: Record<FunnelEvent['source'], string> = {
  opportunity_log: '机会事件',
  signal_queue: '信号队列',
  sim_ledger: '模拟账本',
}

export function createProcessEventRows(events: FunnelEvent[]): ProcessEventRow[] {
  return [...events]
    .sort((left, right) => eventTime(right) - eventTime(left) || (right.sequence ?? 0) - (left.sequence ?? 0))
    .map((event) => ({
      id: event.id,
      symbol: event.symbol,
      market: marketLabels[event.market],
      stage: event.stage,
      result: event.label || event.status,
      source: SOURCE_LABELS[event.source],
      latency: typeof event.latencyMinutes === 'number' ? `${event.latencyMinutes}分钟` : '—',
      reason: event.reason?.trim() || '—',
      timestamp: formatTimestamp(event.at),
    }))
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
