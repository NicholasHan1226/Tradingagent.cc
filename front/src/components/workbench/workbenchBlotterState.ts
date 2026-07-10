import type { HoldingRow, SignalRow } from '../../types/dashboard'
import type { BlotterTab } from './WorkbenchBlotter'
import { resolveTerminalState, selectAvailableTab } from '../../lib/terminalStateResolver'

export function getPreferredTab({ active, positions, completed, review }: { active: SignalRow[]; positions: HoldingRow[]; completed: SignalRow[]; review: SignalRow[] }): BlotterTab {
  return resolveTerminalState({ signals: [...active, ...completed, ...review], positions }).preferredTab
}

export function getAvailableTab(current: BlotterTab, rows: { active: SignalRow[]; positions: HoldingRow[]; completed: SignalRow[]; review: SignalRow[] }) {
  const resolved = resolveTerminalState({ signals: [...rows.active, ...rows.completed, ...rows.review], positions: rows.positions })
  return selectAvailableTab(current, resolved)
}

export function getAvailableTabByCounts(current: BlotterTab, counts: Record<BlotterTab, number>): BlotterTab {
  if (counts[current] > 0) return current
  return (['active', 'completed', 'positions', 'review'] as BlotterTab[]).find((tab) => counts[tab] > 0) ?? 'active'
}
