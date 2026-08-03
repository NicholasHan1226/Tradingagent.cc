import { lstat, readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { isAbsolute, resolve } from 'node:path'
import { assertTrackingUniverse, TRADING_COPILOT_TRACKING_UNIVERSE_ROUTE } from '../copilot/trackingUniverse.ts'

type Options = { trackingUniversePath?: string; workspaceRoot: string }
const MAX_TRACKING_UNIVERSE_BYTES = 500_000

export function createTradingCopilotTrackingUniverseHandler({ trackingUniversePath, workspaceRoot }: Options) {
  const path = resolveTrackingUniversePath({ trackingUniversePath, workspaceRoot })
  return async function handle(req: IncomingMessage, res: ServerResponse) {
    const pathname = new URL(req.url ?? '/', 'http://localhost').pathname
    if (pathname !== TRADING_COPILOT_TRACKING_UNIVERSE_ROUTE) return false
    if (req.method !== 'GET') return sendJson(res, 405, { error: 'Tracking universe API is read-only. Use GET.' })
    try {
      const metadata = await lstat(path)
      if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > MAX_TRACKING_UNIVERSE_BYTES) throw new Error('tracking_universe_file_invalid')
      const bytes = await readFile(path)
      const payload = JSON.parse(bytes.toString('utf8')) as unknown
      assertTrackingUniverse(payload)
      return sendJson(res, 200, payload)
    } catch {
      return sendJson(res, 404, { error: 'Verified TradingCopilot tracking universe unavailable.' })
    }
  }
}

function resolveTrackingUniversePath({ trackingUniversePath, workspaceRoot }: Options) {
  const configured = trackingUniversePath ?? process.env.TRADING_COPILOT_TRACKING_UNIVERSE_PATH
  if (!configured) return resolve(workspaceRoot, 'runtime/tradingcopilot/tracking-universe.json')
  if (!isAbsolute(configured)) throw new Error('TRADING_COPILOT_TRACKING_UNIVERSE_PATH must be absolute')
  return resolve(configured)
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(body))
  return true
}
