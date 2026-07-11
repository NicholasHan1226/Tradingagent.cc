import type { FunnelEvent, MarketSummary, SignalRow } from '../types/dashboard'
import type { DashboardState, DomainStatus } from '../types/status'

export type RuntimeHeartbeatState = 'live' | 'idle' | 'stale' | 'degraded'

export type RuntimeHeartbeat = {
  state: RuntimeHeartbeatState
  headline: string
  detail: string
  runningCount: number
  latestEventLabel: string
  snapshotLabel: string
  tone: 'positive' | 'muted' | 'warning' | 'negative'
}

const SNAPSHOT_STALE_MS = 15 * 60 * 1000
const DEGRADED_STATUSES = new Set<DomainStatus>(['error', 'live-gated'])

export function createRuntimeHeartbeat({
  domains,
  funnelEvents,
  generatedAt,
  marketSummary,
  now = new Date(),
  signals,
}: {
  domains: DashboardState['domains']
  funnelEvents: FunnelEvent[]
  generatedAt: string | null
  marketSummary?: MarketSummary
  now?: Date
  signals: SignalRow[]
}): RuntimeHeartbeat {
  const runningCount = signals.filter((signal) => signal.status === 'pending').length
  const snapshotAge = ageMs(generatedAt, now)
  const degraded = marketSummary?.executionFault === true || Object.values(domains).some((domain) => DEGRADED_STATUSES.has(domain.status))
  const stale = snapshotAge === null || snapshotAge > SNAPSHOT_STALE_MS || Object.values(domains).some((domain) => domain.status === 'stale')
  const latestEventAt = funnelEvents.reduce<string | null>((latest, event) => newerTimestamp(latest, event.at), null)
  const latestEventLabel = latestEventAt ? `最近事件 ${formatAge(ageMs(latestEventAt, now))}` : '尚无过程事件'
  const snapshotLabel = generatedAt ? `快照 ${formatAge(snapshotAge)}` : '等待快照'

  if (degraded) return { state: 'degraded', headline: '证据读取异常 · 需要关注', detail: latestEventLabel, runningCount, latestEventLabel, snapshotLabel, tone: 'negative' }
  if (stale) return { state: 'stale', headline: '快照滞后 · 等待更新', detail: latestEventLabel, runningCount, latestEventLabel, snapshotLabel, tone: 'warning' }
  if (runningCount > 0) return { state: 'live', headline: `自动过程运行中 · ${runningCount}项`, detail: latestEventLabel, runningCount, latestEventLabel, snapshotLabel, tone: 'positive' }
  return { state: 'idle', headline: '调度正常 · 当前空闲', detail: latestEventLabel, runningCount, latestEventLabel, snapshotLabel, tone: 'muted' }
}

const TERMINAL_LABELS: Record<string, string> = {
  buy: '买入观察',
  sell: '卖出观察',
  hold: '继续观察',
  empty: '等待数据',
  signal_queue: '信号队列',
  sim_ledger: '模拟账本',
  opportunity_log: '机会事件',
}

export function translateTerminalValue(value: string | undefined) {
  if (!value) return '—'
  return TERMINAL_LABELS[value.trim().toLowerCase()] ?? value
}

function newerTimestamp(current: string | null, candidate?: string) {
  if (!candidate || !Number.isFinite(new Date(candidate).getTime())) return current
  if (!current) return candidate
  return new Date(candidate).getTime() > new Date(current).getTime() ? candidate : current
}

function ageMs(value: string | null, now: Date) {
  if (!value) return null
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return null
  return Math.max(0, now.getTime() - timestamp)
}

function formatAge(value: number | null) {
  if (value === null) return '时间未知'
  if (value < 60_000) return '刚刚'
  if (value < 3_600_000) return `${Math.floor(value / 60_000)}分钟前`
  if (value < 86_400_000) return `${Math.floor(value / 3_600_000)}小时前`
  return `${Math.floor(value / 86_400_000)}天前`
}
