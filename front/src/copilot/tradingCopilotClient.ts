import { emptyTradingCopilotState, type TradingCopilotState } from './types'

export const TRADING_COPILOT_STATE_ROUTE = '/api/trading-copilot/state'
const LOCAL_DRAFT_KEY = 'trading-copilot:user-declared-state:v1'

export type CopilotPersistence = 'server' | 'local_draft'

export async function loadTradingCopilotState(): Promise<{ state: TradingCopilotState; persistence: CopilotPersistence }> {
  try {
    const response = await fetch(TRADING_COPILOT_STATE_ROUTE, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`state request failed: ${response.status}`)
    return { state: await response.json() as TradingCopilotState, persistence: 'server' }
  } catch {
    const draft = window.localStorage.getItem(LOCAL_DRAFT_KEY)
    if (draft) return { state: JSON.parse(draft) as TradingCopilotState, persistence: 'local_draft' }
    return { state: emptyTradingCopilotState(), persistence: 'local_draft' }
  }
}

export async function saveTradingCopilotState(state: TradingCopilotState): Promise<CopilotPersistence> {
  try {
    const response = await fetch(TRADING_COPILOT_STATE_ROUTE, {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(state),
    })
    if (!response.ok) throw new Error(`state save failed: ${response.status}`)
    window.localStorage.removeItem(LOCAL_DRAFT_KEY)
    return 'server'
  } catch {
    window.localStorage.setItem(LOCAL_DRAFT_KEY, JSON.stringify(state))
    return 'local_draft'
  }
}
