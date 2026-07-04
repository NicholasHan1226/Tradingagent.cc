import type { TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'
import type { HoldingRow, PerformancePoint, SignalRow } from '../types/dashboard'

export function getSnapshotSignals(snapshot: TradingAgentReadModelSnapshot | null, fallback: SignalRow[]) {
  if (!snapshot) return fallback
  return snapshot.signals
}

export function getSnapshotPerformance(snapshot: TradingAgentReadModelSnapshot | null, fallback: PerformancePoint[]) {
  if (!snapshot) return fallback
  return snapshot.performance
}

export function getSnapshotHoldings(snapshot: TradingAgentReadModelSnapshot | null, fallback: HoldingRow[]) {
  if (!snapshot) return fallback
  return snapshot.holdings
}

export function hasSnapshotRows(snapshot: TradingAgentReadModelSnapshot | null, domain: 'performance' | 'signals' | 'holdings') {
  if (!snapshot) return false
  return snapshot[domain].length > 0
}
