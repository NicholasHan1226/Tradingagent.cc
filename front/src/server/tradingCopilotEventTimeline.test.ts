import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { createTradingCopilotEventTimelineHandler } from './tradingCopilotEventTimeline'

const servers: ReturnType<typeof createServer>[] = []
const symbol = '000400.SZ'

afterEach(() => { servers.splice(0).forEach((server) => server.close()) })

async function serveTimeline(timeline: Record<string, unknown>, receipt: Record<string, unknown>) {
  const workspaceRoot = await mkdtemp(join(tmpdir(), 'copilot-event-timeline-'))
  const projectionDir = join(workspaceRoot, 'projection')
  const path = join(projectionDir, `${symbol}.json`)
  const receiptPath = join(projectionDir, `${symbol}.receipt.json`)
  await mkdir(projectionDir)
  await writeFile(path, JSON.stringify(timeline))
  await writeFile(receiptPath, JSON.stringify(receipt))
  const handler = createTradingCopilotEventTimelineHandler({
    workspaceRoot,
    projectionDir,
    now: () => new Date('2026-08-03T02:00:00.000Z'),
  })
  const server = createServer((req, res) => { void handler(req, res) })
  servers.push(server)
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('server_address_unavailable')
  return `http://127.0.0.1:${address.port}`
}

function timeline() {
  return {
    contractId: 'tradingagent.trading_copilot_event_timeline.v1',
    symbol,
    generatedAt: '2026-08-03T01:00:00.000Z',
    validUntil: '2026-08-04T01:00:00.000Z',
    events: [{ sourceReceiptId: 'source-1', sourceReceiptSha256: 'a'.repeat(64) }],
    coverage: { acceptedEventCount: 1, acceptedReceiptIds: ['source-1'], blockedDatasetIds: [], blockedDatasetReasons: {}, sentimentLabelsInvented: false },
  }
}

function receiptFor(value: Record<string, unknown>) {
  const bytes = Buffer.from(JSON.stringify(value))
  return {
    contractId: 'tradingagent.trading_copilot_event_timeline_receipt.v1',
    symbol,
    timelineSha256: createHash('sha256').update(bytes).digest('hex'),
    generatedAt: value.generatedAt,
    validUntil: value.validUntil,
    sourceReceipts: [{ receiptId: 'source-1', receiptSha256: 'a'.repeat(64) }],
  }
}

describe('TradingCopilot event timeline projection', () => {
  it('serves only a current, receipt-bound event timeline', async () => {
    const value = timeline()
    const base = await serveTimeline(value, receiptFor(value))

    expect((await fetch(`${base}/api/trading-copilot/event-timeline?symbol=${symbol}`)).status).toBe(200)
    expect((await fetch(`${base}/api/trading-copilot/event-timeline?symbol=../../secret`)).status).toBe(400)
    expect((await fetch(`${base}/api/trading-copilot/event-timeline?symbol=${symbol}`, { method: 'POST' })).status).toBe(405)
  })

  it('rejects expired or source-mismatched detached receipts', async () => {
    const value = timeline()
    const expired = { ...receiptFor(value), validUntil: '2020-01-01T00:00:00.000Z' }
    const expiredBase = await serveTimeline(value, expired)
    expect((await fetch(`${expiredBase}/api/trading-copilot/event-timeline?symbol=${symbol}`)).status).toBe(404)

    const mismatched = { ...receiptFor(value), sourceReceipts: [{ receiptId: 'other', receiptSha256: 'b'.repeat(64) }] }
    const mismatchedBase = await serveTimeline(value, mismatched)
    expect((await fetch(`${mismatchedBase}/api/trading-copilot/event-timeline?symbol=${symbol}`)).status).toBe(404)
  })
})
