import type { TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import type { PerformancePoint, SignalRow } from '../types/dashboard'

export function getSnapshotSignals(snapshot: TradingAgentReadModelSnapshot | null, fallback: SignalRow[]) {
  return snapshot?.signals?.length ? snapshot.signals : fallback
}

export function getSnapshotPerformance(snapshot: TradingAgentReadModelSnapshot | null, fallback: PerformancePoint[]) {
  return snapshot?.performance?.length ? snapshot.performance : fallback
}
