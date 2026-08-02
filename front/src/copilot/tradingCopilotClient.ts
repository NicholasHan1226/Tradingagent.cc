import { emptyTradingCopilotState, type TradingCopilotState } from './types'

export const TRADING_COPILOT_STATE_ROUTE = '/api/trading-copilot/state'
const LOCAL_DRAFT_KEY = 'trading-copilot:user-declared-state:v1'
const EMPTY_HEAD = 'empty'

export type CopilotPersistence = 'server' | 'local_draft'
export type LoadedCopilotState = { state: TradingCopilotState; persistence: CopilotPersistence; headSha256: string; message?: string }
export type SavedCopilotState = {
  persistence: CopilotPersistence
  headSha256: string
  conflict: boolean
  currentState?: TradingCopilotState
  message?: string
}

export async function loadTradingCopilotState(): Promise<LoadedCopilotState> {
  try {
    const response = await fetch(TRADING_COPILOT_STATE_ROUTE, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`state request failed: ${response.status}`)
    const state = await response.json() as TradingCopilotState
    const headSha256 = readEtag(response)
    const draft = readLocalDraft()
    if (draft?.baseHeadSha256 === headSha256) return { state: draft.state, persistence: 'local_draft', headSha256, message: '检测到尚未同步的浏览器草稿' }
    if (draft) return { state, persistence: 'server', headSha256, message: '服务器状态已变化，浏览器旧草稿未自动覆盖服务器' }
    return { state, persistence: 'server', headSha256 }
  } catch {
    const draft = readLocalDraft()
    if (draft) return { state: draft.state, persistence: 'local_draft', headSha256: draft.baseHeadSha256, message: '服务器不可用，已载入此浏览器的未同步草稿' }
    return { state: emptyTradingCopilotState(), persistence: 'local_draft', headSha256: EMPTY_HEAD }
  }
}

function readLocalDraft(): { state: TradingCopilotState; baseHeadSha256: string } | null {
  const raw = window.localStorage.getItem(LOCAL_DRAFT_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as TradingCopilotState | { state: TradingCopilotState; baseHeadSha256?: string }
    const envelope = 'state' in parsed ? parsed : { state: parsed, baseHeadSha256: EMPTY_HEAD }
    if (!isStateShape(envelope.state)) throw new Error('invalid local state')
    return { state: envelope.state, baseHeadSha256: envelope.baseHeadSha256 ?? EMPTY_HEAD }
  } catch {
    window.localStorage.removeItem(LOCAL_DRAFT_KEY)
    return null
  }
}

function isStateShape(value: unknown): value is TradingCopilotState {
  if (!value || typeof value !== 'object') return false
  const state = value as Partial<TradingCopilotState>
  return state.schemaVersion === 1 && state.source === 'user_declared' && Boolean(state.account) && Array.isArray(state.holdings) && Array.isArray(state.watchlist) && Array.isArray(state.decisions)
}

export async function saveTradingCopilotState(state: TradingCopilotState, expectedHeadSha256: string): Promise<SavedCopilotState> {
  try {
    const response = await fetch(TRADING_COPILOT_STATE_ROUTE, {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'If-Match': `"${expectedHeadSha256}"` },
      body: JSON.stringify(state),
    })
    if (response.status === 409) {
      const body = await response.json() as { state?: TradingCopilotState; headSha256?: string; error?: string }
      return {
        persistence: 'server', conflict: true, headSha256: body.headSha256 ?? readEtag(response),
        currentState: body.state, message: body.error ?? '个人状态已在其他标签页更新',
      }
    }
    if (!response.ok) throw new Error(`state save failed: ${response.status}`)
    window.localStorage.removeItem(LOCAL_DRAFT_KEY)
    return { persistence: 'server', conflict: false, headSha256: readEtag(response) }
  } catch (error) {
    window.localStorage.setItem(LOCAL_DRAFT_KEY, JSON.stringify({ state, baseHeadSha256: expectedHeadSha256 }))
    return {
      persistence: 'local_draft', conflict: false, headSha256: expectedHeadSha256,
      message: error instanceof Error ? error.message : '服务器保存失败，已保留本机草稿',
    }
  }
}

function readEtag(response: Response) {
  return response.headers.get('ETag')?.replace(/^W\//, '').replace(/^"|"$/g, '') || EMPTY_HEAD
}
