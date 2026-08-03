import { describe, expect, it } from 'vitest'
import { resolveCoverageStatus } from './coverageStatus'
import type { StockIntelligence } from './stockIntelligence'
import type { CopilotAnalysis } from './types'

const observation = { mode: 'tradingagent_observation' } as CopilotAnalysis

function intelligence(freshness: 'fresh' | 'stale' | 'degraded' | 'demo', verified: StockIntelligence['verification']['status'] = 'verified') {
  return {
    mode: 'tradingagent_observation',
    verification: { status: verified },
    source: { freshness },
  } as StockIntelligence
}

describe('coverage status projection', () => {
  it('classifies only a verified, fresh formal projection as formal_fresh', () => {
    expect(resolveCoverageStatus({ analysis: observation, intelligence: intelligence('fresh') })).toMatchObject({
      status: 'formal_fresh',
      label: '正式且新鲜',
    })
  })

  it('keeps valid non-fresh or snapshot-only observations out of formal coverage', () => {
    expect(resolveCoverageStatus({ analysis: observation, intelligence: intelligence('stale') }).status).toBe('observation_only')
    expect(resolveCoverageStatus({ analysis: observation }).status).toBe('observation_only')
  })

  it('fails closed for demo, unavailable, or unverified client outcomes', () => {
    expect(resolveCoverageStatus({ analysis: { ...observation, mode: 'demo_fixture' } }).status).toBe('coverage_gap')
    expect(resolveCoverageStatus({ analysis: { ...observation, mode: 'analysis_unavailable' } }).status).toBe('coverage_gap')
    expect(resolveCoverageStatus({ analysis: observation, intelligence: intelligence('fresh', 'demo') }).status).toBe('coverage_gap')
  })
})
