import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { TRADING_AGENT_SNAPSHOT_ROUTE, getTradingAgentSnapshotResponse } from '../api/tradingAgentIntegration.ts'
import type { TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel.ts'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot.ts'

type SnapshotHttpServerOptions = {
  allowedOrigins?: string[]
  apiToken?: string
  readSnapshot?: () => Promise<TradingAgentReadModelSnapshot>
  workspaceRoot?: string
}

const currentDir = dirname(fileURLToPath(import.meta.url))
const defaultWorkspaceRoot = resolve(currentDir, '../../..')

export function createTradingAgentSnapshotHttpServer(options: SnapshotHttpServerOptions = {}) {
  const handler = createSnapshotRequestHandler(options)
  return createServer((req, res) => {
    void handler(req, res)
  })
}

export function createSnapshotRequestHandler({
  allowedOrigins = parseList(process.env.TRADING_AGENT_SNAPSHOT_CORS_ORIGINS),
  apiToken = process.env.TRADING_AGENT_SNAPSHOT_API_TOKEN,
  workspaceRoot = process.env.FINANCE_WORKSPACE_ROOT ?? defaultWorkspaceRoot,
  readSnapshot = () => readTradingAgentSnapshot({ workspaceRoot }),
}: SnapshotHttpServerOptions = {}) {
  return async function handleSnapshotRequest(req: IncomingMessage, res: ServerResponse) {
    const url = new URL(req.url ?? '/', 'http://localhost')
    const origin = req.headers.origin

    if (!applyCors({ allowedOrigins, origin, req, res })) return

    if (req.method === 'OPTIONS') {
      sendJson(res, 204, null)
      return
    }

    if (url.pathname === '/healthz' || url.pathname === '/health') {
      sendJson(res, 200, { ok: true, service: 'trading-agent-snapshot-api' })
      return
    }

    if (url.pathname !== TRADING_AGENT_SNAPSHOT_ROUTE) {
      sendJson(res, 404, { error: 'Not found' })
      return
    }

    if (req.method !== 'GET') {
      sendJson(res, 405, { error: 'TradingAgent snapshot API is read-only. Use GET.' })
      return
    }

    if (!isAuthorized(req, apiToken)) {
      res.setHeader('WWW-Authenticate', 'Bearer')
      sendJson(res, 401, { error: 'Unauthorized' })
      return
    }

    const response = await getTradingAgentSnapshotResponse(readSnapshot)
    res.statusCode = response.status
    response.headers.forEach((value, key) => res.setHeader(key, value))
    res.setHeader('Cache-Control', response.headers.get('Cache-Control') ?? 'no-store')
    res.setHeader('Content-Type', 'application/json')
    res.end(await response.text())
  }
}

function applyCors({
  allowedOrigins,
  origin,
  req,
  res,
}: {
  allowedOrigins: string[]
  origin: string | undefined
  req: IncomingMessage
  res: ServerResponse
}) {
  res.setHeader('Vary', 'Origin')
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS')
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type')

  if (!origin) return true
  if (allowedOrigins.includes('*') || allowedOrigins.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    return true
  }

  sendJson(res, req.method === 'OPTIONS' ? 204 : 403, req.method === 'OPTIONS' ? null : { error: 'Origin not allowed' })
  return false
}

function isAuthorized(req: IncomingMessage, apiToken: string | undefined) {
  if (!apiToken) return true
  return req.headers.authorization === `Bearer ${apiToken}`
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', 'application/json')
  res.end(body === null ? '' : JSON.stringify(body))
}

function parseList(value: string | undefined) {
  return (value ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

if (isMainModule()) {
  const host = process.env.TRADING_AGENT_SNAPSHOT_HOST ?? '127.0.0.1'
  const port = Number(process.env.TRADING_AGENT_SNAPSHOT_PORT ?? 8787)
  const server = createTradingAgentSnapshotHttpServer()
  server.listen(port, host, () => {
    console.log(`TradingAgent snapshot API listening on http://${host}:${port}`)
  })
}

function isMainModule() {
  const entry = process.argv[1]
  if (!entry) return false
  return fileURLToPath(import.meta.url) === entry
}
