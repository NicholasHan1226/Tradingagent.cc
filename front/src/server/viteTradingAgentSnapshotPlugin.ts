import type { IncomingMessage, ServerResponse } from 'node:http'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Plugin } from 'vite'
import { getTradingAgentSnapshotResponse, TRADING_AGENT_SNAPSHOT_ROUTE } from '../api/tradingAgentIntegration.ts'
import type { TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel.ts'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot.ts'

type MiddlewareOptions = {
  workspaceRoot?: string
  readSnapshot?: () => Promise<TradingAgentReadModelSnapshot>
}

type RequestLike = Pick<IncomingMessage, 'method' | 'url'>
type ResponseLike = {
  statusCode: number
  setHeader: (key: string, value: string | number | readonly string[]) => unknown
  end: (value?: unknown) => unknown
}
type ViteMiddleware = (req: IncomingMessage, res: ServerResponse, next: () => void) => void | Promise<void>

const currentDir = dirname(fileURLToPath(import.meta.url))
const defaultWorkspaceRoot = resolve(currentDir, '../../..')

export function createTradingAgentSnapshotMiddleware({
  workspaceRoot = process.env.FINANCE_WORKSPACE_ROOT ?? defaultWorkspaceRoot,
  readSnapshot = () => readTradingAgentSnapshot({ workspaceRoot }),
}: MiddlewareOptions = {}) {
  return async function tradingAgentSnapshotMiddleware(req: RequestLike, res: ResponseLike, next: () => void) {
    const pathname = new URL(req.url ?? '/', 'http://localhost').pathname

    if (pathname !== TRADING_AGENT_SNAPSHOT_ROUTE) {
      next()
      return
    }

    if (req.method !== 'GET') {
      res.statusCode = 405
      res.setHeader('Content-Type', 'application/json')
      res.setHeader('Cache-Control', 'no-store')
      res.end(JSON.stringify({ error: 'TradingAgent snapshot is read-only. Use GET.' }))
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

export function tradingAgentSnapshotPlugin(options: MiddlewareOptions = {}): Plugin {
  return {
    name: 'trading-agent-snapshot',
    configureServer(server) {
      server.middlewares.use(createTradingAgentSnapshotMiddleware(options) as ViteMiddleware)
    },
    configurePreviewServer(server) {
      server.middlewares.use(createTradingAgentSnapshotMiddleware(options) as ViteMiddleware)
    },
  }
}
