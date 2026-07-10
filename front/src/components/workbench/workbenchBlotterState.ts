import type { HoldingRow, SignalRow } from '../../types/dashboard'
import type { BlotterTab } from './WorkbenchBlotter'

export function getPreferredTab({ active, positions, completed, review }: { active: SignalRow[]; positions: HoldingRow[]; completed: SignalRow[]; review: SignalRow[] }): BlotterTab {
  if (active.length) return 'active'
  if (completed.length) return 'completed'
  if (positions.length) return 'positions'
  if (review.length) return 'review'
  return 'active'
}
