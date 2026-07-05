import type { HoldingRow, Market, PerformancePoint, SignalRow } from '../types/dashboard'
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

export function getLivePerformanceData(now: Date, rows: PerformancePoint[], animateLatest = false) {
  if (!rows.length) return rows
  if (!animateLatest) return rows

  const seconds = now.getSeconds()
  const liveMove = (Math.sin(seconds / 4) + Math.cos(seconds / 7)) * 0.16

  return rows.map((point, index) => {
    if (index !== rows.length - 1) return point

    return {
      ...point,
      simulated: Number((point.simulated + liveMove).toFixed(2)),
      opportunity: Number((point.opportunity + liveMove * 0.2).toFixed(2)),
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
      { label: '发现', rows: discovered },
      { label: '评分', rows: formed },
      { label: '风控', rows: conditioned },
      { label: '待执行', rows: riskPassed },
      { label: '结果', rows: tradeSignals },
    ],
    tradeSignals,
    executed: tradeSignals.filter((signal) => signal.status === 'executed'),
    pending: tradeSignals.filter((signal) => signal.status === 'pending'),
    missed: tradeSignals.filter((signal) => signal.status === 'missed'),
    blocked: conditioned.filter((signal) => signal.status === 'blocked'),
    cancelled: conditioned.filter((signal) => signal.status === 'cancelled'),
  }
}

export function getHomeOutcome(signals: SignalRow[], holdings: HoldingRow[]) {
  const actionable = getActionableSignals(signals)
  const closed = getClosedSignals(signals)
  const leadSignal = actionable.find((signal) => signal.status === 'pending') ?? actionable[0]
  const blockedSignal = signals.find((signal) => signal.status === 'blocked' || signal.status === 'cancelled')
  const reviewSignal = closed.find((signal) => signal.status === 'missed') ?? closed[0]
  const leadingHolding = holdings[0]

  return {
    leadSignal,
    blockedSignal,
    reviewSignal,
    leadingHolding,
  }
}

function signalStageRank(signal: SignalRow) {
  if (signal.stage === '成交' || signal.stage === '错过') return 4
  if (signal.stage === '待执行') return 3
  if (signal.stage === '风控' || signal.stage === '拒绝') return 2
  if (signal.stage === '评分') return 1
  if (signal.stage === '发现') return 0
  if (signal.steps >= 6) return 4
  if (signal.steps >= 5) return 3
  if (signal.steps >= 4) return 2
  if (signal.steps >= 2) return 1
  return 0
}
