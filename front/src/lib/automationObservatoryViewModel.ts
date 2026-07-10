import type { HoldingRow, LegacyPage, Page, SignalRow } from '../types/dashboard'
import type { WorkbenchViewModel } from './workbenchViewModel'
import { resolveTerminalState } from './terminalStateResolver'
export type { AutomationRuntimeItem, AutomationRuntimeKind } from './terminalStateResolver'

export type AutomationObservatoryViewModel = {
  running: SignalRow[]
  positions: HoldingRow[]
  completed: SignalRow[]
  automaticReview: SignalRow[]
  runtimeItem: AutomationRuntimeItem
  summary: {
    runningCount: number
    positionCount: number
    completedCount: number
    automaticReviewCount: number
  }
}

const PAGE_ALIASES: Record<LegacyPage, Page> = {
  主页: '总览',
  机会: '过程',
  决策: '过程',
}

export function normalizePage(page: Page | LegacyPage): Page {
  return page in PAGE_ALIASES ? PAGE_ALIASES[page as LegacyPage] : page as Page
}

export function createAutomationObservatoryViewModel(
  workbench: WorkbenchViewModel,
): AutomationObservatoryViewModel {
  const resolved = resolveTerminalState({
    signals: [...workbench.opportunities.active, ...workbench.opportunities.completed, ...workbench.reviewItems],
    positions: workbench.positions,
  })

  return {
    running: resolved.running,
    positions: resolved.positions,
    completed: resolved.completed,
    automaticReview: resolved.review,
    runtimeItem: resolved.runtimeItem,
    summary: {
      runningCount: resolved.counts.running,
      positionCount: resolved.counts.positions,
      completedCount: resolved.counts.completed,
      automaticReviewCount: resolved.counts.review,
    },
  }
}
