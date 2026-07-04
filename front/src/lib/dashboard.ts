import type { Market, PerformancePoint, SignalRow } from '../types/dashboard'
import type { DomainHealth } from '../types/status'

export function getActionableSignals(rows: SignalRow[]) {
  const actionable = rows.filter((signal) => signal.status === 'pending' || signal.status === 'blocked')
  return actionable.length ? actionable : rows
}

export function getClosedSignals(rows: SignalRow[]) {
  const closed = rows.filter((signal) => signal.status === 'executed' || signal.status === 'missed' || signal.status === 'cancelled')
  return closed.length ? closed : rows
}

export function getVisibleSignals(rows: SignalRow[], activeMarket: Market) {
  const filteredRows = rows.filter((signal) => activeMarket === 'All Markets' || signal.market === activeMarket)
  return filteredRows.length ? filteredRows : rows
}

export function getLivePerformanceData(now: Date, rows: PerformancePoint[]) {
  const seconds = now.getSeconds()
  const liveMove = (Math.sin(seconds / 4) + Math.cos(seconds / 7)) * 0.16

  return rows.map((point, index) => {
    if (index !== rows.length - 1) return point

    return {
      ...point,
      simulated: Number((9.42 + liveMove).toFixed(2)),
      opportunity: Number((-2.55 + liveMove * 0.2).toFixed(2)),
    }
  })
}

export function isStaleHealth(health: DomainHealth, maxAgeMs: number, now = new Date()) {
  return now.getTime() - new Date(health.updatedAt).getTime() > maxAgeMs
}

export function getSignalFunnel(rows: SignalRow[]) {
  const discovered = rows
  const formed = rows.filter((signal) => signalStageRank(signal) >= 1)
  const conditioned = rows.filter((signal) => signalStageRank(signal) >= 2)
  const riskPassed = rows.filter((signal) => signalStageRank(signal) >= 3 && signal.status !== 'blocked' && signal.status !== 'cancelled')
  const tradeSignals = riskPassed.filter((signal) => signal.status === 'executed' || signal.status === 'pending' || signal.status === 'missed')

  return {
    stages: [
      { label: '发现机会', rows: discovered },
      { label: '形成信号', rows: formed },
      { label: '交易条件', rows: conditioned },
      { label: '风险筛选', rows: riskPassed },
      { label: '交易信号', rows: tradeSignals },
    ],
    tradeSignals,
    executed: tradeSignals.filter((signal) => signal.status === 'executed'),
    pending: tradeSignals.filter((signal) => signal.status === 'pending'),
    missed: tradeSignals.filter((signal) => signal.status === 'missed'),
    blocked: conditioned.filter((signal) => signal.status === 'blocked'),
    cancelled: conditioned.filter((signal) => signal.status === 'cancelled'),
  }
}

function signalStageRank(signal: SignalRow) {
  if (signal.stage === '执行确认') return 4
  if (signal.stage === '风险筛选') return 3
  if (signal.stage === '交易条件') return 2
  if (signal.stage === '形成信号') return 1
  if (signal.stage === '发现机会') return 0
  if (signal.steps >= 6) return 4
  if (signal.steps >= 5) return 3
  if (signal.steps >= 4) return 2
  if (signal.steps >= 2) return 1
  return 0
}
