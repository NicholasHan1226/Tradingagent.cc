import { describe, expect, it, vi } from 'vitest'
import { loadStockIntelligence } from './stockIntelligenceClient'
import { collectDatasetActivities } from './datasetActivity'

const activityAuthority = {
  datasetId: 'daily', market: 'ashare', timezone: 'Asia/Shanghai',
  calendar: { id: 'sse', version: 'v1', sourceDatasetId: 'cn.market.trade_calendar', receiptId: 'calendar-receipt', receiptSha256: 'e'.repeat(64), lineageSha256: 'f'.repeat(64), calendarSha256: '1'.repeat(64) },
  session: { state: 'open', asOf: '2026-08-02T01:00:10.000Z' },
  dataThrough: '2026-08-02T01:00:00.000Z',
  source: { receiptId: 'source-receipt', receiptSha256: 'b'.repeat(64), lineageSha256: 'c'.repeat(64) },
}

const projection = {
  symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
  verification: { status: 'verified', receiptId: 'projection-receipt', projectionSha256: 'a'.repeat(64), validUntil: '2099-08-03T01:00:00.000Z', verifiedAt: '2026-08-02T01:01:00.000Z', verifierId: 'test-verifier/v1' },
  source: { transportContract: 'tradingdatas_v1_catalog_query', datasetId: 'daily', receiptId: 'source-receipt', receiptSha256: 'b'.repeat(64), lineageSha256: 'c'.repeat(64), dataThrough: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:00:10.000Z', freshness: 'fresh', adjustment: 'forward', activityAuthority },
  marketRules: { board: 'main', lotSize: 100, tPlusOne: true, priceLimitPct: 10, stStatus: 'normal', tradingStatus: 'trading', session: 'closed', corporateActionAdjusted: true },
  analysis: { symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', generatedAt: '2026-08-02T01:00:00.000Z', evidenceStrength: { value: 72, label: '正式定型证据强度', semantics: 'typed_evidence_strength_v1', contractVersion: 'v1', sourceRefs: ['source-receipt'], asOf: '2026-08-02T01:00:00.000Z' }, readiness: { data: 'verified', evidence: 'typed', model: 'ready', action: 'eligible_for_human_review', reasons: ['测试证据已定型'] }, verdict: '等待条件', summary: '正式投影', support: [], oppose: [], buyConditions: ['量价确认'], invalidation: ['结构失效'] },
  quote: { price: 31, previousClose: 30, change: 1, changePct: 3.33, open: 30.5, high: 31.2, low: 30.2, volume: 100, turnoverRate: 1, peTtm: 20, marketCapCny: 1_000_000 },
  company: { exchange: 'SZ', industry: '电网设备', area: '河南', listingDate: '1997-04-18', description: '正式投影' },
  series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] },
  forecast: null, events: [],
}

function formalEvent(activityAuthority = {
  ...projection.source.activityAuthority,
  datasetId: 'cn.dataset.anns_d',
  session: { state: 'closed', asOf: '2026-08-02T01:00:10.000Z' },
  source: { receiptId: 'event-receipt', receiptSha256: 'd'.repeat(64), lineageSha256: 'e'.repeat(64) },
}) {
  return {
    id: 'news-1', kind: 'news', title: '已绑定新闻', summary: 'test', source: 'test', sourceClass: 'professional_news', sourceConfidence: 'medium', publishedAt: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:01:00.000Z', revisedAt: null, novelty: 'new', sentiment: 'neutral', sentimentConfidence: null, impactDirection: 'uncertain', impactHorizon: 'short_term', relatedSymbols: ['000400.SZ'], url: 'https://example.com/news', sourceReceiptId: 'event-receipt', sourceReceiptSha256: 'd'.repeat(64), contentSha256: 'f'.repeat(64),
    dataCapability: { inputContract: 'tradingagent.trading_copilot_projection_batch_input.v2', transportContract: 'tradingdatas_v1_catalog_query', datasetId: 'cn.dataset.anns_d', catalogVersion: 'v1', asOf: '2026-08-02T01:01:00.000Z', dataThrough: '2026-08-02T01:00:00.000Z', freshness: 'fresh', receiptId: 'event-receipt', receiptSha256: 'd'.repeat(64), lineageSha256: 'e'.repeat(64), activityAuthority },
  }
}

describe('stock intelligence client', () => {
  it('accepts a symbol-bound formal projection', async () => {
    const fetcher = vi.fn(async () => Response.json(projection)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toMatchObject({ mode: 'tradingagent_observation' })
  })

  it('requires a receipt-bound activity authority and maps it per dataset', async () => {
    const fetcher = vi.fn(async () => Response.json(projection)) as unknown as typeof fetch
    const loaded = await loadStockIntelligence('000400.SZ', fetcher)
    expect(loaded).not.toBeNull()
    expect(collectDatasetActivities(loaded!).find((activity) => activity.datasetId === 'daily')).toMatchObject({ state: 'live', clockKey: 'ashare/Asia/Shanghai/sse' })

    const missing = { ...projection, source: { ...projection.source, activityAuthority: undefined } }
    const missingFetcher = vi.fn(async () => Response.json(missing)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', missingFetcher)).resolves.toBeNull()
  })

  it.each([
    ['dataset id', { ...activityAuthority, datasetId: 'other.dataset' }],
    ['data through', { ...activityAuthority, dataThrough: '2026-08-02T01:00:01.000Z' }],
    ['source receipt sha', { ...activityAuthority, source: { ...activityAuthority.source, receiptSha256: '0'.repeat(64) } }],
    ['source lineage sha', { ...activityAuthority, source: { ...activityAuthority.source, lineageSha256: '0'.repeat(64) } }],
    ['calendar content proof', { ...activityAuthority, calendar: { ...activityAuthority.calendar, calendarSha256: 'not-a-sha' } }],
    ['calendar receipt lineage', { ...activityAuthority, calendar: { ...activityAuthority.calendar, lineageSha256: 'not-a-sha' } }],
    ['timezone-aware session asOf', { ...activityAuthority, session: { ...activityAuthority.session, asOf: '2026-08-02T01:00:10' } }],
  ])('fails closed when authority %s is incomplete or unequal', async (_case, authority) => {
    const forged = { ...projection, source: { ...projection.source, activityAuthority: authority } }
    const fetcher = vi.fn(async () => Response.json(forged)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toBeNull()
  })

  it('maps a receipt-bound event authority and rejects an unequal event binding', async () => {
    const event = formalEvent()
    const fetcher = vi.fn(async () => Response.json({ ...projection, events: [event] })) as unknown as typeof fetch
    const loaded = await loadStockIntelligence('000400.SZ', fetcher)
    expect(collectDatasetActivities(loaded!).find((activity) => activity.datasetId === 'cn.dataset.anns_d')).toMatchObject({ state: 'closed', clockKey: 'ashare/Asia/Shanghai/sse' })

    const mismatched = formalEvent({
      ...event.dataCapability.activityAuthority,
      source: { ...event.dataCapability.activityAuthority.source, receiptSha256: '0'.repeat(64) },
    })
    const mismatchedFetcher = vi.fn(async () => Response.json({ ...projection, events: [mismatched] })) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', mismatchedFetcher)).resolves.toBeNull()
  })

  it('fails closed on a projection for another symbol', async () => {
    const fetcher = vi.fn(async () => Response.json(projection)) as unknown as typeof fetch
    await expect(loadStockIntelligence('600519.SH', fetcher)).resolves.toBeNull()
  })

  it('rejects a self-reported ready forecast that does not match its evidence', async () => {
    const forged = {
      ...projection,
      forecast: {
        mode: 'calibrated_research', horizon: '1d', horizonLabel: '未来1日', directionalView: '偏强', modelId: 'kronos_challenger',
        evidence: { sourceMode: 'formal_observation', horizon: '1d', modelId: 'kronos_challenger', modelManifestBound: true, modelManifestId: null, modelManifestSha256: null, pointInTimeVerified: true, pointInTimeReceiptId: null, pointInTimeReceiptSha256: null, frozenOosReceiptBound: true, frozenOosReceiptId: null, frozenOosReceiptSha256: null, calibrationProofAccepted: false, calibrationReceiptId: null, calibrationReceiptSha256: null, effectiveIndependentSamples: 80, intervalCoverageVerified: true, intervalCoverageReceiptId: null, intervalCoverageReceiptSha256: null, costPolicyBound: true, costPolicyId: null, costPolicySha256: null, baselineComparisonAccepted: false, baselineComparisonReceiptId: null, baselineComparisonReceiptSha256: null, postCostUtilityPositive: false },
        readiness: { status: 'decision_support_ready', usableFor: 'manual_decision_support', horizon: '1d', modelId: 'kronos_challenger', gates: [], probabilitiesVisible: true, intervalsMayUseCoverageLabels: true },
        takeaway: 'forged', drivers: [], caveat: 'forged',
      },
    }
    const fetcher = vi.fn(async () => Response.json(forged)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toBeNull()
  })

  it('rejects formal events without source and content receipt bindings', async () => {
    const forged = { ...projection, events: [{ id: 'news-1', kind: 'news', title: '未绑定新闻', summary: 'test', source: 'test', sourceClass: 'professional_news', sourceConfidence: 'medium', publishedAt: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:01:00.000Z', revisedAt: null, novelty: 'new', sentiment: 'neutral', sentimentConfidence: 0.5, impactDirection: 'uncertain', impactHorizon: 'short_term', relatedSymbols: ['000400.SZ'], url: 'https://example.com/news', sourceReceiptId: null, sourceReceiptSha256: null, contentSha256: null }] }
    const fetcher = vi.fn(async () => Response.json(forged)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toBeNull()
  })

  it('rejects formal events without their catalog/query capability provenance', async () => {
    const event = {
      id: 'news-1', kind: 'news', title: '已绑定新闻', summary: 'test', source: 'test', sourceClass: 'professional_news', sourceConfidence: 'medium', publishedAt: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:01:00.000Z', revisedAt: null, novelty: 'new', sentiment: 'neutral', sentimentConfidence: null, impactDirection: 'uncertain', impactHorizon: 'short_term', relatedSymbols: ['000400.SZ'], url: 'https://example.com/news', sourceReceiptId: 'event-receipt', sourceReceiptSha256: 'c'.repeat(64), contentSha256: 'd'.repeat(64), dataCapability: null,
    }
    const fetcher = vi.fn(async () => Response.json({ ...projection, events: [event] })) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toBeNull()
  })
})
