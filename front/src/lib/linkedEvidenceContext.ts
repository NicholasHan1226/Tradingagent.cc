import { marketLabels } from '../data/dashboard'
import type { FunnelEvent } from '../types/dashboard'

const STAGES: FunnelEvent['stage'][] = ['发现', '研判', '风控', '待确认', '结果']

export type LinkedEvidenceContextModel = {
  id: string
  symbol: string
  market: string
  stage: string
  result: string
  evidence: string
  eventCount: number
  updatedAt: string
}

export function createLinkedEvidenceContext(events: FunnelEvent[], opportunityId: string | null): LinkedEvidenceContextModel | null {
  if (!opportunityId) return null
  const related = filterEventsByOpportunity(events, opportunityId)
  if (!related.length) return null
  const ordered = [...related].sort((left, right) => eventTime(left) - eventTime(right) || (left.sequence ?? 0) - (right.sequence ?? 0))
  const latest = ordered[ordered.length - 1]
  const stages = new Set(ordered.map((event) => event.stage))
  return {
    id: opportunityId,
    symbol: latest.symbol,
    market: marketLabels[latest.market],
    stage: latest.stage,
    result: latest.label || latest.status,
    evidence: `${STAGES.filter((stage) => stages.has(stage)).length}/5 阶段`,
    eventCount: ordered.length,
    updatedAt: formatTimestamp(latest.at),
  }
}

export function filterEventsByOpportunity(events: FunnelEvent[], opportunityId: string | null) {
  if (!opportunityId) return events
  return events.filter((event) => event.opportunityId === opportunityId)
}

function eventTime(event: FunnelEvent) {
  if (!event.at) return 0
  const value = new Date(event.at).getTime()
  return Number.isFinite(value) ? value : 0
}

function formatTimestamp(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date)
}
