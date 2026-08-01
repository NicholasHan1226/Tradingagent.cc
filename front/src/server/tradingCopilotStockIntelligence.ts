import { readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { resolve, sep } from 'node:path'
import { assertStockIntelligenceProjection, TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE } from '../copilot/stockIntelligenceClient.ts'

type Options = { projectionDir?: string; workspaceRoot: string }
const symbolPattern = /^(?:0|3|6)\d{5}\.(?:SZ|SH)$/

export function createTradingCopilotStockIntelligenceHandler({ projectionDir, workspaceRoot }: Options) {
  const root = resolve(projectionDir ?? resolve(workspaceRoot, 'runtime/tradingcopilot/stock-intelligence'))
  return async function handle(req: IncomingMessage, res: ServerResponse) {
    const url = new URL(req.url ?? '/', 'http://localhost')
    if (url.pathname !== TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE) return false
    if (req.method !== 'GET') return sendJson(res, 405, { error: 'Stock intelligence API is read-only. Use GET.' })
    const symbol = (url.searchParams.get('symbol') ?? '').toUpperCase()
    if (!symbolPattern.test(symbol)) return sendJson(res, 400, { error: 'Invalid A-share symbol.' })
    const path = resolve(root, `${symbol}.json`)
    if (!path.startsWith(`${root}${sep}`)) return sendJson(res, 400, { error: 'Invalid projection path.' })
    try {
      const payload: unknown = JSON.parse(await readFile(path, 'utf8'))
      assertStockIntelligenceProjection(payload, symbol)
      return sendJson(res, 200, payload)
    } catch {
      return sendJson(res, 404, { error: 'Verified stock intelligence projection unavailable.' })
    }
  }
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(body))
  return true
}
