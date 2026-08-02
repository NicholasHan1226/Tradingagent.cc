import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { resolve, sep } from 'node:path'
import { assertStockIntelligenceProjection, TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE } from '../copilot/stockIntelligenceClient.ts'

type Options = { projectionDir?: string; workspaceRoot: string; now?: () => Date }
const symbolPattern = /^(?:0|3|6)\d{5}\.(?:SZ|SH)$/
const MAX_PROJECTION_BYTES = 5_000_000

type DetachedProjectionReceipt = {
  contractId: 'tradingagent.trading_copilot_stock_projection_receipt.v1'
  symbol: string
  receiptId: string
  projectionSha256: string
  generatedAt: string
  validUntil: string
  verifierId: string
  verifierVersion: string
  sourceReceipts: Array<{ receiptId: string; receiptSha256: string }>
}

export function createTradingCopilotStockIntelligenceHandler({ projectionDir, workspaceRoot, now = () => new Date() }: Options) {
  const root = resolve(projectionDir ?? resolve(workspaceRoot, 'runtime/tradingcopilot/stock-intelligence'))
  return async function handle(req: IncomingMessage, res: ServerResponse) {
    const url = new URL(req.url ?? '/', 'http://localhost')
    if (url.pathname !== TRADING_COPILOT_STOCK_INTELLIGENCE_ROUTE) return false
    if (req.method !== 'GET') return sendJson(res, 405, { error: 'Stock intelligence API is read-only. Use GET.' })
    const symbol = (url.searchParams.get('symbol') ?? '').toUpperCase()
    if (!symbolPattern.test(symbol)) return sendJson(res, 400, { error: 'Invalid A-share symbol.' })
    const path = resolve(root, `${symbol}.json`)
    const receiptPath = resolve(root, `${symbol}.receipt.json`)
    if (!path.startsWith(`${root}${sep}`)) return sendJson(res, 400, { error: 'Invalid projection path.' })
    try {
      const [projectionBytes, receiptBytes] = await Promise.all([readFile(path), readFile(receiptPath)])
      if (projectionBytes.byteLength > MAX_PROJECTION_BYTES || receiptBytes.byteLength > MAX_PROJECTION_BYTES) throw new Error('projection_too_large')
      const payload = JSON.parse(projectionBytes.toString('utf8')) as Record<string, unknown>
      const receipt = JSON.parse(receiptBytes.toString('utf8')) as DetachedProjectionReceipt
      verifyDetachedReceipt(receipt, payload, projectionBytes, symbol, now())
      const verifiedPayload = {
        ...payload,
        verification: {
          status: 'verified', receiptId: receipt.receiptId, projectionSha256: receipt.projectionSha256,
          validUntil: receipt.validUntil, verifiedAt: now().toISOString(), verifierId: `${receipt.verifierId}@${receipt.verifierVersion}`,
        },
      }
      assertStockIntelligenceProjection(verifiedPayload, symbol)
      return sendJson(res, 200, verifiedPayload)
    } catch {
      return sendJson(res, 404, { error: 'Verified stock intelligence projection unavailable.' })
    }
  }
}

function verifyDetachedReceipt(receipt: DetachedProjectionReceipt, payload: Record<string, unknown>, bytes: Buffer, symbol: string, now: Date) {
  const sha256 = createHash('sha256').update(bytes).digest('hex')
  if (receipt.contractId !== 'tradingagent.trading_copilot_stock_projection_receipt.v1' || receipt.symbol !== symbol || payload.symbol !== symbol) throw new Error('projection_receipt_identity_invalid')
  if (!receipt.receiptId?.trim() || !receipt.verifierId?.trim() || !receipt.verifierVersion?.trim()) throw new Error('projection_receipt_verifier_invalid')
  if (receipt.projectionSha256 !== sha256 || !isSha256(receipt.projectionSha256)) throw new Error('projection_receipt_hash_invalid')
  if (!isTimestamp(receipt.generatedAt) || !isTimestamp(receipt.validUntil) || Date.parse(receipt.generatedAt) > now.getTime() || Date.parse(receipt.validUntil) <= now.getTime()) throw new Error('projection_receipt_time_invalid')
  if (!Array.isArray(receipt.sourceReceipts) || !receipt.sourceReceipts.length || receipt.sourceReceipts.some((item) => !item.receiptId?.trim() || !isSha256(item.receiptSha256))) throw new Error('projection_receipt_sources_invalid')
  const source = payload.source as { receiptId?: unknown; receiptSha256?: unknown } | undefined
  if (!source || !receipt.sourceReceipts.some((item) => item.receiptId === source.receiptId && item.receiptSha256 === source.receiptSha256)) throw new Error('projection_receipt_source_binding_invalid')
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(body))
  return true
}
