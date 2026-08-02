import { beforeEach, describe, expect, it, vi } from 'vitest'
import { emptyTradingCopilotState } from './types'
import { loadTradingCopilotState, saveTradingCopilotState } from './tradingCopilotClient'

beforeEach(() => {
  window.localStorage.clear()
  vi.unstubAllGlobals()
})

describe('TradingCopilot state client', () => {
  it('sends If-Match and surfaces a server conflict without claiming a save', async () => {
    const state = emptyTradingCopilotState('2026-08-02T00:00:00.000Z')
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('If-Match')).toBe('"head-a"')
      return Response.json({ error: 'changed', state, headSha256: 'head-b' }, { status: 409, headers: { ETag: '"head-b"' } })
    })
    vi.stubGlobal('fetch', fetcher)
    await expect(saveTradingCopilotState(state, 'head-a')).resolves.toMatchObject({ conflict: true, persistence: 'server', headSha256: 'head-b', currentState: state })
  })

  it('keeps an unavailable-server save as an explicit browser draft', async () => {
    const state = emptyTradingCopilotState('2026-08-02T00:00:00.000Z')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(saveTradingCopilotState(state, 'head-a')).resolves.toMatchObject({ conflict: false, persistence: 'local_draft', headSha256: 'head-a' })
    const stored = JSON.parse(window.localStorage.getItem('trading-copilot:user-declared-state:v1') ?? '{}')
    expect(stored).toMatchObject({ baseHeadSha256: 'head-a', state: { source: 'user_declared' } })
  })

  it('does not let a stale browser draft silently overwrite newer server state', async () => {
    const serverState = emptyTradingCopilotState('2026-08-02T00:00:00.000Z')
    const draftState = { ...serverState, ownerId: 'draft-owner' }
    window.localStorage.setItem('trading-copilot:user-declared-state:v1', JSON.stringify({ state: draftState, baseHeadSha256: 'old-head' }))
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(serverState, { headers: { ETag: '"new-head"' } })))
    await expect(loadTradingCopilotState()).resolves.toMatchObject({ state: serverState, persistence: 'server', headSha256: 'new-head', message: expect.stringContaining('旧草稿') })
  })
})
