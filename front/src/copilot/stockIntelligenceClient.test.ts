import { describe, expect, it, vi } from 'vitest'
import { loadStockIntelligence } from './stockIntelligenceClient'

const projection = {
  symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
  verification: { status: 'verified', receiptId: 'projection-receipt', projectionSha256: 'a'.repeat(64), validUntil: '2099-08-03T01:00:00.000Z', verifiedAt: '2026-08-02T01:01:00.000Z', verifierId: 'test-verifier/v1' },
  source: { datasetId: 'daily', receiptId: 'source-receipt', receiptSha256: 'b'.repeat(64), dataThrough: '2026-08-02T01:00:00.000Z', retrievedAt: '2026-08-02T01:00:10.000Z', freshness: 'fresh', adjustment: 'forward' },
  marketRules: { board: 'main', lotSize: 100, tPlusOne: true, priceLimitPct: 10, stStatus: 'normal', tradingStatus: 'trading', session: 'closed', corporateActionAdjusted: true },
  analysis: { symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', generatedAt: '2026-08-02T01:00:00.000Z', evidenceStrength: { value: 72, label: '正式定型证据强度', semantics: 'typed_evidence_strength_v1', contractVersion: 'v1', sourceRefs: ['source-receipt'], asOf: '2026-08-02T01:00:00.000Z' }, readiness: { data: 'verified', evidence: 'typed', model: 'ready', action: 'eligible_for_human_review', reasons: ['测试证据已定型'] }, verdict: '等待条件', summary: '正式投影', support: [], oppose: [], buyConditions: ['量价确认'], invalidation: ['结构失效'] },
  quote: { price: 31, previousClose: 30, change: 1, changePct: 3.33, open: 30.5, high: 31.2, low: 30.2, volume: 100, turnoverRate: 1, peTtm: 20, marketCapCny: 1_000_000 },
  company: { exchange: 'SZ', industry: '电网设备', area: '河南', listingDate: '1997-04-18', description: '正式投影' },
  series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] },
  forecast: null, events: [],
}

describe('stock intelligence client', () => {
  it('accepts a symbol-bound formal projection', async () => {
    const fetcher = vi.fn(async () => Response.json(projection)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toMatchObject({ mode: 'tradingagent_observation' })
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
})
