import { marketLabels } from '../data/dashboard'
import type { FunnelEvent, HoldingRow, SignalRow } from '../types/dashboard'

const STAGES: FunnelEvent['stage'][] = ['发现', '研判', '风控', '待确认', '结果']

export type LinkedEvidenceContextModel = {
  id: string
  symbol: string
  market: string
  stage: string
  result: string
  evidence: string
  eventCount: number
  signalCount: number
  holdingCount: number
  attributablePnl?: number
  updatedAt: string
  legacyFrozen?: boolean
}

export function createLinkedEvidenceContext(events: FunnelEvent[], opportunityId: string | null, signals: SignalRow[] = [], holdings: HoldingRow[] = []): LinkedEvidenceContextModel | null {
  if (!opportunityId) return null
  const related = filterEventsByOpportunity(events, opportunityId)
  if (!related.length) return null
  const ordered = [...related].sort((left, right) => eventTime(left) - eventTime(right) || (left.sequence ?? 0) - (right.sequence ?? 0))
  const latest = ordered[ordered.length - 1]
  const stages = new Set(ordered.map((event) => event.stage))
  const legacyFrozen = ordered.some((event) => event.source === 'legacy_frozen_opportunity_log')
  const relatedSignals = legacyFrozen ? [] : signals.filter((signal) => signal.opportunityId === opportunityId)
  const relatedHoldings = legacyFrozen ? [] : holdings.filter((holding) => holding.opportunityId === opportunityId)
  const pnlValues = relatedHoldings
    .map((holding) => holding.realizedPnl === undefined && holding.unrealizedPnl === undefined ? undefined : (holding.realizedPnl ?? 0) + (holding.unrealizedPnl ?? 0))
    .filter((value): value is number => value !== undefined)
  return {
    id: opportunityId,
    symbol: latest.symbol,
    market: marketLabels[latest.market],
    stage: latest.stage,
    result: latest.label || latest.status,
    evidence: `${STAGES.filter((stage) => stages.has(stage)).length}/5 阶段`,
    eventCount: ordered.length,
    signalCount: relatedSignals.length,
    holdingCount: relatedHoldings.length,
    attributablePnl: pnlValues.length ? roundMoney(pnlValues.reduce((total, value) => total + value, 0)) : undefined,
    updatedAt: formatTimestamp(latest.at),
    legacyFrozen,
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

function roundMoney(value: number) { return Math.round(value * 100) / 100 }
