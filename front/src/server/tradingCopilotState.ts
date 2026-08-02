import { createHash, randomUUID } from 'node:crypto'
import { appendFile, mkdir, readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { dirname, isAbsolute, resolve } from 'node:path'
import { emptyTradingCopilotState, isAshareSymbol, type TradingCopilotState } from '../copilot/types.ts'

export const TRADING_COPILOT_STATE_ROUTE = '/api/trading-copilot/state'
const MAX_STATE_BYTES = 1_000_000

export type TradingCopilotStateOptions = {
  statePath?: string
  workspaceRoot: string
}

type StateEvent = {
  sequence?: number
  eventId: string
  recordedAt: string
  previousSha256: string | null
  stateSha256: string
  eventSha256?: string
  state: TradingCopilotState
}

type LedgerSnapshot = { state: TradingCopilotState; headSha256: string; sequence: number }
const EMPTY_HEAD = 'empty'
const stateLocks = new Map<string, Promise<void>>()

export function createTradingCopilotStateHandler(options: TradingCopilotStateOptions) {
  const statePath = resolveStatePath(options)

  return async function handleTradingCopilotState(req: IncomingMessage, res: ServerResponse) {
    const pathname = new URL(req.url ?? '/', 'http://localhost').pathname
    if (pathname !== TRADING_COPILOT_STATE_ROUTE) return false

    if (req.method === 'GET') {
      try {
        const snapshot = await readLedger(statePath)
        setStateHeaders(res, snapshot)
        sendJson(res, 200, snapshot.state)
      } catch {
        sendJson(res, 503, { error: 'TradingCopilot state ledger integrity check failed' })
      }
      return true
    }

    if (req.method === 'PUT') {
      try {
        const state = validateState(await readJsonBody(req))
        const expectedHead = parseIfMatch(req.headers['if-match'])
        if (!expectedHead) {
          sendJson(res, 428, { error: 'If-Match state head is required' })
          return true
        }
        const result = await withStateLock(statePath, async () => {
          const current = await readLedger(statePath)
          if (expectedHead !== current.headSha256) return { conflict: true as const, current }
          return { conflict: false as const, snapshot: await appendState(statePath, state, current) }
        })
        if (result.conflict) {
          setStateHeaders(res, result.current)
          sendJson(res, 409, { error: 'TradingCopilot state changed in another tab or session', state: result.current.state, headSha256: result.current.headSha256 })
          return true
        }
        setStateHeaders(res, result.snapshot)
        sendJson(res, 200, { ok: true, state, headSha256: result.snapshot.headSha256 })
      } catch (error) {
        sendJson(res, 400, { error: error instanceof Error ? error.message : 'Invalid TradingCopilot state' })
      }
      return true
    }

    sendJson(res, 405, { error: 'TradingCopilot state supports GET and PUT only.' })
    return true
  }
}

export async function readLatestState(statePath: string): Promise<TradingCopilotState> {
  return (await readLedger(statePath)).state
}

async function appendState(statePath: string, state: TradingCopilotState, previous: LedgerSnapshot): Promise<LedgerSnapshot> {
  await mkdir(dirname(statePath), { recursive: true, mode: 0o700 })
  const serialized = stableStateJson(state)
  const core = {
    sequence: previous.sequence + 1,
    eventId: randomUUID(),
    recordedAt: new Date().toISOString(),
    previousSha256: previous.headSha256 === EMPTY_HEAD ? null : previous.headSha256,
    stateSha256: createHash('sha256').update(serialized).digest('hex'),
    state,
  }
  const event: StateEvent = { ...core, eventSha256: createHash('sha256').update(stableJson(core)).digest('hex') }
  await appendFile(statePath, `${JSON.stringify(event)}\n`, { encoding: 'utf8', mode: 0o600 })
  return { state, headSha256: event.stateSha256, sequence: core.sequence }
}

async function readLedger(statePath: string): Promise<LedgerSnapshot> {
  try {
    const content = await readFile(statePath, 'utf8')
    const lines = content.split('\n').filter(Boolean)
    if (!lines.length) return { state: emptyTradingCopilotState(), headSha256: EMPTY_HEAD, sequence: 0 }
    let previousStateSha256: string | null = null
    let latestState = emptyTradingCopilotState()
    lines.forEach((line, index) => {
      const event = JSON.parse(line) as StateEvent
      const state = validateState(event.state)
      const canonicalStateSha256 = createHash('sha256').update(stableStateJson(state)).digest('hex')
      const legacyStateSha256 = createHash('sha256').update(JSON.stringify(state)).digest('hex')
      if (event.stateSha256 !== canonicalStateSha256 && event.stateSha256 !== legacyStateSha256) throw new Error(`TradingCopilot ledger state hash mismatch at line ${index + 1}`)
      if (event.previousSha256 !== previousStateSha256) throw new Error(`TradingCopilot ledger chain mismatch at line ${index + 1}`)
      if (event.sequence !== undefined && event.sequence !== index + 1) throw new Error(`TradingCopilot ledger sequence mismatch at line ${index + 1}`)
      if (event.eventSha256) {
        const core = { sequence: event.sequence, eventId: event.eventId, recordedAt: event.recordedAt, previousSha256: event.previousSha256, stateSha256: event.stateSha256, state: event.state }
        const eventSha256 = createHash('sha256').update(stableJson(core)).digest('hex')
        if (event.eventSha256 !== eventSha256) throw new Error(`TradingCopilot ledger event hash mismatch at line ${index + 1}`)
      }
      previousStateSha256 = event.stateSha256
      latestState = state
    })
    return { state: latestState, headSha256: previousStateSha256 ?? EMPTY_HEAD, sequence: lines.length }
  } catch (error) {
    if (isMissingFile(error)) return { state: emptyTradingCopilotState(), headSha256: EMPTY_HEAD, sequence: 0 }
    throw error
  }
}

function validateState(value: unknown): TradingCopilotState {
  if (!value || typeof value !== 'object') throw new Error('State must be an object')
  const state = value as Partial<TradingCopilotState>
  if (state.schemaVersion !== 1 || state.source !== 'user_declared') throw new Error('Unsupported state contract')
  if (typeof state.ownerId !== 'string' || !state.ownerId.trim()) throw new Error('ownerId is required')
  if (!isTimestamp(state.updatedAt)) throw new Error('updatedAt must be an ISO timestamp')
  if (!state.account || !isFiniteNonNegative(state.account.declaredCapitalCny) || !isFiniteNonNegative(state.account.availableCashCny)) {
    throw new Error('Account values must be non-negative numbers')
  }
  if (state.account.availableCashCny > state.account.declaredCapitalCny) throw new Error('Available cash cannot exceed declared capital')
  if (!isTimestamp(state.account.updatedAt)) throw new Error('Account updatedAt must be an ISO timestamp')
  if (!Array.isArray(state.holdings) || !Array.isArray(state.watchlist) || !Array.isArray(state.decisions)) throw new Error('State lists are required')
  const holdings = state.holdings
  const watchlist = state.watchlist
  const decisions = state.decisions
  if (new Set(holdings.map((item) => item.symbol)).size !== holdings.length || new Set(watchlist.map((item) => item.symbol)).size !== watchlist.length || new Set(decisions.map((item) => item.id)).size !== decisions.length) throw new Error('State lists contain duplicate identities')
  for (const holding of holdings) {
    if (!isAshareSymbol(holding.symbol) || !holding.name?.trim()) throw new Error('Holding must use a valid A-share symbol and name')
    if (!Number.isInteger(holding.quantity) || !Number.isInteger(holding.sellableQuantity) || holding.quantity < 0 || holding.sellableQuantity < 0 || holding.sellableQuantity > holding.quantity) throw new Error('Holding quantities are invalid')
    if (!isFiniteNonNegative(holding.averageCost) || !isTimestamp(holding.updatedAt)) throw new Error('Holding cost or timestamp is invalid')
  }
  for (const item of watchlist) {
    if (!isAshareSymbol(item.symbol) || !item.name?.trim() || !isTimestamp(item.addedAt)) throw new Error('Watchlist item is invalid')
  }
  if (holdings.some((holding) => !watchlist.some((item) => item.symbol === holding.symbol))) throw new Error('Every holding must remain on the watchlist')
  for (const decision of decisions) {
    if (!decision.id || !isAshareSymbol(decision.symbol) || !['planned', 'observing', 'skipped'].includes(decision.action) || decision.authority !== 'human_intent_only' || !isTimestamp(decision.recordedAt)) throw new Error('Decision record is invalid')
    if (decision.plan) {
      if (!decision.plan.reason.trim() || !decision.plan.trigger.trim() || !decision.plan.invalidation.trim() || (decision.plan.maxRiskCny !== null && !isFiniteNonNegative(decision.plan.maxRiskCny))) throw new Error('Decision plan is invalid')
    }
    if (decision.review) {
      if (!['pending', 'executed', 'not_executed', 'expired'].includes(decision.review.status) || !decision.review.actualAction.trim() || !decision.review.note.trim() || !isTimestamp(decision.review.reviewedAt)) throw new Error('Decision review is invalid')
    }
  }
  return state as TradingCopilotState
}

async function readJsonBody(req: IncomingMessage) {
  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_STATE_BYTES) throw new Error('State payload is too large')
    chunks.push(buffer)
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown
}

function resolveStatePath({ statePath, workspaceRoot }: TradingCopilotStateOptions) {
  const configured = statePath ?? process.env.TRADING_COPILOT_STATE_PATH
  if (!configured) return resolve(workspaceRoot, 'runtime/tradingcopilot/state-events.jsonl')
  if (!isAbsolute(configured)) throw new Error('TRADING_COPILOT_STATE_PATH must be absolute')
  return resolve(configured)
}

function stableStateJson(state: TradingCopilotState) {
  return stableJson(state)
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  const record = value as Record<string, unknown>
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(',')}}`
}

function parseIfMatch(value: string | string[] | undefined) {
  const raw = Array.isArray(value) ? value[0] : value
  return raw?.trim().replace(/^W\//, '').replace(/^"|"$/g, '') || null
}

function setStateHeaders(res: ServerResponse, snapshot: LedgerSnapshot) {
  res.setHeader('ETag', `"${snapshot.headSha256}"`)
  res.setHeader('X-Trading-Copilot-Revision', String(snapshot.sequence))
}

async function withStateLock<T>(path: string, operation: () => Promise<T>): Promise<T> {
  const previous = stateLocks.get(path) ?? Promise.resolve()
  let release: () => void = () => {}
  const current = new Promise<void>((resolve) => { release = resolve })
  const queued = previous.then(() => current)
  stateLocks.set(path, queued)
  await previous
  try {
    return await operation()
  } finally {
    release()
    if (stateLocks.get(path) === queued) stateLocks.delete(path)
  }
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

function isFiniteNonNegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isMissingFile(error: unknown) {
  return Boolean(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(body))
}
