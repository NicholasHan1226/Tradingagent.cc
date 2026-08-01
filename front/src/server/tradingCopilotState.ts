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
  eventId: string
  recordedAt: string
  previousSha256: string | null
  stateSha256: string
  state: TradingCopilotState
}

export function createTradingCopilotStateHandler(options: TradingCopilotStateOptions) {
  const statePath = resolveStatePath(options)

  return async function handleTradingCopilotState(req: IncomingMessage, res: ServerResponse) {
    const pathname = new URL(req.url ?? '/', 'http://localhost').pathname
    if (pathname !== TRADING_COPILOT_STATE_ROUTE) return false

    if (req.method === 'GET') {
      sendJson(res, 200, await readLatestState(statePath))
      return true
    }

    if (req.method === 'PUT') {
      try {
        const state = validateState(await readJsonBody(req))
        await appendState(statePath, state)
        sendJson(res, 200, { ok: true, state })
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
  try {
    const content = await readFile(statePath, 'utf8')
    const lines = content.split('\n').filter(Boolean)
    if (!lines.length) return emptyTradingCopilotState()
    const event = JSON.parse(lines.at(-1) ?? '') as StateEvent
    return validateState(event.state)
  } catch (error) {
    if (isMissingFile(error)) return emptyTradingCopilotState()
    throw error
  }
}

async function appendState(statePath: string, state: TradingCopilotState) {
  await mkdir(dirname(statePath), { recursive: true, mode: 0o700 })
  const previous = await readLastEvent(statePath)
  const serialized = stableStateJson(state)
  const event: StateEvent = {
    eventId: randomUUID(),
    recordedAt: new Date().toISOString(),
    previousSha256: previous?.stateSha256 ?? null,
    stateSha256: createHash('sha256').update(serialized).digest('hex'),
    state,
  }
  await appendFile(statePath, `${JSON.stringify(event)}\n`, { encoding: 'utf8', mode: 0o600 })
}

async function readLastEvent(statePath: string): Promise<StateEvent | null> {
  try {
    const content = await readFile(statePath, 'utf8')
    const line = content.split('\n').filter(Boolean).at(-1)
    return line ? JSON.parse(line) as StateEvent : null
  } catch (error) {
    if (isMissingFile(error)) return null
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
  for (const holding of state.holdings) {
    if (!isAshareSymbol(holding.symbol) || !holding.name?.trim()) throw new Error('Holding must use a valid A-share symbol and name')
    if (!Number.isInteger(holding.quantity) || !Number.isInteger(holding.sellableQuantity) || holding.quantity < 0 || holding.sellableQuantity < 0 || holding.sellableQuantity > holding.quantity) throw new Error('Holding quantities are invalid')
    if (!isFiniteNonNegative(holding.averageCost) || !isTimestamp(holding.updatedAt)) throw new Error('Holding cost or timestamp is invalid')
  }
  for (const item of state.watchlist) {
    if (!isAshareSymbol(item.symbol) || !item.name?.trim() || !isTimestamp(item.addedAt)) throw new Error('Watchlist item is invalid')
  }
  for (const decision of state.decisions) {
    if (!decision.id || !isAshareSymbol(decision.symbol) || !['planned', 'observing', 'skipped'].includes(decision.action) || decision.authority !== 'human_intent_only' || !isTimestamp(decision.recordedAt)) throw new Error('Decision record is invalid')
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
  return JSON.stringify(state)
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
