import { createHash } from 'node:crypto'
import { lstat, readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { isAbsolute, resolve, sep } from 'node:path'

export const TRADING_COPILOT_EVENT_TIMELINE_ROUTE = '/api/trading-copilot/event-timeline'
const symbol = /^(?:0|3|6)\d{5}\.(?:SZ|SH)$/
const sha256 = /^[a-f0-9]{64}$/

export function createTradingCopilotEventTimelineHandler({ projectionDir, workspaceRoot, now = () => new Date() }: { projectionDir?: string; workspaceRoot: string; now?: () => Date }) {
  const configured = projectionDir ?? process.env.TRADING_COPILOT_EVENT_TIMELINE_DIR
  if (configured && !isAbsolute(configured)) throw new Error('TRADING_COPILOT_EVENT_TIMELINE_DIR must be absolute')
  const root = resolve(configured ?? resolve(workspaceRoot, 'runtime/tradingcopilot/event-timeline'))
  return async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url ?? '/', 'http://localhost')
    if (url.pathname !== TRADING_COPILOT_EVENT_TIMELINE_ROUTE) return false
    if (req.method !== 'GET') return send(res, 405, { error: 'Event timeline API is read-only. Use GET.' })
    const code = (url.searchParams.get('symbol') ?? '').toUpperCase()
    if (!symbol.test(code)) return send(res, 400, { error: 'Invalid A-share symbol.' })
    const path = resolve(root, `${code}.json`)
    const receiptPath = resolve(root, `${code}.receipt.json`)
    if (!path.startsWith(`${root}${sep}`) || !receiptPath.startsWith(`${root}${sep}`)) return send(res, 400, { error: 'Invalid projection path.' })
    try {
      const [rootMeta, meta, receiptMeta] = await Promise.all([lstat(root), lstat(path), lstat(receiptPath)])
      if (!rootMeta.isDirectory() || rootMeta.isSymbolicLink() || !meta.isFile() || meta.isSymbolicLink() || !receiptMeta.isFile() || receiptMeta.isSymbolicLink() || meta.size > 5_000_000 || receiptMeta.size > 1_000_000) throw new Error('file')
      const [bytes, receiptBytes] = await Promise.all([readFile(path), readFile(receiptPath)])
      const value = JSON.parse(bytes.toString()) as Record<string, unknown>
      const receipt = JSON.parse(receiptBytes.toString()) as Record<string, unknown>
      verifyDetachedReceipt(value, receipt, bytes, code, now())
      return send(res, 200, value)
    } catch { return send(res, 404, { error: 'Verified event timeline unavailable.' }) }
  }
}

function verifyDetachedReceipt(value: Record<string, unknown>, receipt: Record<string, unknown>, bytes: Buffer, code: string, now: Date) {
  if (value.contractId !== 'tradingagent.trading_copilot_event_timeline.v1' || value.symbol !== code || receipt.contractId !== 'tradingagent.trading_copilot_event_timeline_receipt.v1' || receipt.symbol !== code) throw new Error('identity')
  if (receipt.timelineSha256 !== createHash('sha256').update(bytes).digest('hex') || !isSha256(receipt.timelineSha256)) throw new Error('hash')
  if (!isTimestamp(value.generatedAt) || !isTimestamp(value.validUntil) || !isTimestamp(receipt.generatedAt) || !isTimestamp(receipt.validUntil) || receipt.generatedAt !== value.generatedAt || receipt.validUntil !== value.validUntil || Date.parse(value.generatedAt) > now.getTime() || Date.parse(value.validUntil) <= now.getTime() || Date.parse(value.validUntil) <= Date.parse(value.generatedAt)) throw new Error('time')
  const eventSources = sourcesFromEvents(value.events)
  const receiptSources = sourcesFromReceipt(receipt.sourceReceipts)
  if (!sameSources(eventSources, receiptSources)) throw new Error('sources')
}

function sourcesFromEvents(value: unknown) {
  if (!Array.isArray(value)) throw new Error('events')
  return canonicalSources(value.map((item) => {
    if (!item || typeof item !== 'object') throw new Error('event')
    const event = item as Record<string, unknown>
    return { receiptId: event.sourceReceiptId, receiptSha256: event.sourceReceiptSha256 }
  }))
}

function sourcesFromReceipt(value: unknown) {
  if (!Array.isArray(value)) throw new Error('receipt_sources')
  return canonicalSources(value)
}

function canonicalSources(value: Array<unknown>) {
  const result = new Set<string>()
  for (const item of value) {
    if (!item || typeof item !== 'object') throw new Error('source')
    const source = item as Record<string, unknown>
    if (typeof source.receiptId !== 'string' || !source.receiptId.trim() || !isSha256(source.receiptSha256)) throw new Error('source')
    result.add(`${source.receiptId}\u0000${source.receiptSha256}`)
  }
  return [...result].sort()
}

function sameSources(left: string[], right: string[]) {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && sha256.test(value)
}

function send(res: ServerResponse, status: number, body: unknown) { res.statusCode = status; res.setHeader('Cache-Control', 'no-store'); res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body)); return true }
