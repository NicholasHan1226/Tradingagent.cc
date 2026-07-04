import type { ChartEvent, PerformancePoint, SignalRow } from '../types/dashboard'

export function deriveChartEvents(performance: PerformancePoint[], signals: SignalRow[]): ChartEvent[] {
  const events: ChartEvent[] = []
  const positive = findLargestMove(performance, (move) => move > 0)
  const negative = findLargestMove(performance, (move) => move < 0)
  const currentOpportunity = signals.find((signal) => signal.status === 'pending' || signal.status === 'blocked')
  const latest = performance[performance.length - 1]

  if (positive) {
    events.push({
      day: positive.day,
      title: '收益跃升',
      targetPage: '决策',
      summary: `当日收益变化 +${positive.move.toFixed(2)}%`,
    })
  }

  if (negative) {
    events.push({
      day: negative.day,
      title: '回撤收缩',
      targetPage: '风险',
      summary: `当日收益变化 ${negative.move.toFixed(2)}%`,
    })
  }

  if (latest && currentOpportunity) {
    events.push({
      day: latest.day,
      title: currentOpportunity.symbol,
      targetPage: '机会',
      summary: currentOpportunity.reason,
    })
  }

  return dedupeEvents(events).slice(0, 4)
}

function findLargestMove(performance: PerformancePoint[], predicate: (move: number) => boolean) {
  return performance.slice(1).reduce<{ day: string; move: number } | null>((best, point, index) => {
    const previous = performance[index]
    const move = Number((point.simulated - previous.simulated).toFixed(2))
    if (!predicate(move)) return best
    if (!best || Math.abs(move) >= Math.abs(best.move)) return { day: point.day, move }
    return best
  }, null)
}

function dedupeEvents(events: ChartEvent[]) {
  const seen = new Set<string>()
  return events.filter((event) => {
    const key = `${event.day}:${event.targetPage}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
