import { describe, expect, it, vi } from 'vitest'
import { loadStockIntelligence } from './stockIntelligenceClient'

const projection = {
  symbol: '000400.SZ', name: '许继电气', mode: 'tradingagent_observation', updatedAt: '2026-08-02T01:00:00.000Z',
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
        evidence: { sourceMode: 'formal_observation', horizon: '1d', modelId: 'kronos_challenger', modelManifestBound: true, pointInTimeVerified: true, frozenOosReceiptBound: true, calibrationProofAccepted: false, effectiveIndependentSamples: 80, intervalCoverageVerified: true, costPolicyBound: true },
        readiness: { status: 'decision_support_ready', usableFor: 'manual_decision_support', horizon: '1d', modelId: 'kronos_challenger', gates: [], probabilitiesVisible: true, intervalsMayUseCoverageLabels: true },
        takeaway: 'forged', drivers: [], caveat: 'forged',
      },
    }
    const fetcher = vi.fn(async () => Response.json(forged)) as unknown as typeof fetch
    await expect(loadStockIntelligence('000400.SZ', fetcher)).resolves.toBeNull()
  })
})
