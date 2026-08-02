import { describe, expect, it } from 'vitest'
import { assessForecastReadiness, MIN_CALIBRATION_EFFECTIVE_SAMPLES, type ForecastEvidence } from './forecastReadiness'

const readyEvidence: ForecastEvidence = {
  sourceMode: 'formal_observation', horizon: '1d', modelId: 'linear_ridge_baseline',
  modelManifestBound: true, modelManifestId: 'manifest-v1', modelManifestSha256: 'a'.repeat(64),
  pointInTimeVerified: true, pointInTimeReceiptId: 'pit-v1', pointInTimeReceiptSha256: 'b'.repeat(64),
  frozenOosReceiptBound: true, frozenOosReceiptId: 'oos-v1', frozenOosReceiptSha256: 'c'.repeat(64),
  calibrationProofAccepted: true, calibrationReceiptId: 'cal-v1', calibrationReceiptSha256: 'd'.repeat(64),
  effectiveIndependentSamples: MIN_CALIBRATION_EFFECTIVE_SAMPLES,
  intervalCoverageVerified: true, intervalCoverageReceiptId: 'coverage-v1', intervalCoverageReceiptSha256: 'e'.repeat(64),
  costPolicyBound: true, costPolicyId: 'cost-v1', costPolicySha256: 'f'.repeat(64),
  baselineComparisonAccepted: true, baselineComparisonReceiptId: 'baseline-v1', baselineComparisonReceiptSha256: '1'.repeat(64),
  postCostUtilityPositive: true,
}

describe('forecast readiness gate', () => {
  it('allows probability and coverage labels only after every gate passes', () => {
    const result = assessForecastReadiness(readyEvidence)
    expect(result.status).toBe('decision_support_ready')
    expect(result.usableFor).toBe('manual_decision_support')
    expect(result.probabilitiesVisible).toBe(true)
    expect(result.gates.every((gate) => gate.passed)).toBe(true)
  })

  it('keeps demo forecasts illustrative and hides probability semantics', () => {
    const result = assessForecastReadiness({
      ...readyEvidence, sourceMode: 'demo_fixture', calibrationProofAccepted: false,
      effectiveIndependentSamples: 0, intervalCoverageVerified: false, postCostUtilityPositive: false,
    })
    expect(result.status).toBe('illustrative_only')
    expect(result.usableFor).toBe('visual_illustration')
    expect(result.probabilitiesVisible).toBe(false)
    expect(result.intervalsMayUseCoverageLabels).toBe(false)
  })

  it('does not let the Kronos model name bypass missing evidence', () => {
    const result = assessForecastReadiness({
      ...readyEvidence, modelId: 'kronos_challenger', pointInTimeVerified: false,
    })
    expect(result.status).toBe('blocked')
    expect(result.usableFor).toBe('none')
    expect(result.probabilitiesVisible).toBe(false)
  })

  it('fails closed below the effective independent sample threshold', () => {
    const result = assessForecastReadiness({
      ...readyEvidence, effectiveIndependentSamples: MIN_CALIBRATION_EFFECTIVE_SAMPLES - 1,
    })
    expect(result.status).toBe('blocked')
    expect(result.gates.find((gate) => gate.id === 'effective_samples')?.passed).toBe(false)
  })
})
