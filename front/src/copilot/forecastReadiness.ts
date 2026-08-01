export const forecastHorizons = ['m30', 'm60', 'close', '1d', '3d', '5d'] as const

export type ForecastHorizon = typeof forecastHorizons[number]
export type ForecastModelId = 'naive_last_value' | 'linear_ridge_baseline' | 'kronos_challenger'
export type ForecastReadinessStatus = 'illustrative_only' | 'decision_support_ready' | 'blocked'

export type ForecastEvidence = {
  sourceMode: 'demo_fixture' | 'formal_observation'
  horizon: ForecastHorizon
  modelId: ForecastModelId
  modelManifestBound: boolean
  pointInTimeVerified: boolean
  frozenOosReceiptBound: boolean
  calibrationProofAccepted: boolean
  effectiveIndependentSamples: number
  intervalCoverageVerified: boolean
  costPolicyBound: boolean
}

export type ForecastReadinessGate = {
  id: 'formal_observation' | 'explicit_horizon' | 'model_manifest' | 'point_in_time' | 'frozen_oos'
    | 'calibration' | 'effective_samples' | 'interval_coverage' | 'cost_policy'
  label: string
  passed: boolean
}

export type ForecastReadiness = {
  status: ForecastReadinessStatus
  usableFor: 'visual_illustration' | 'manual_decision_support' | 'none'
  horizon: ForecastHorizon
  modelId: ForecastModelId
  gates: ForecastReadinessGate[]
  probabilitiesVisible: boolean
  intervalsMayUseCoverageLabels: boolean
}

export const MIN_CALIBRATION_EFFECTIVE_SAMPLES = 40

export function assessForecastReadiness(evidence: ForecastEvidence): ForecastReadiness {
  const gates: ForecastReadinessGate[] = [
    gate('formal_observation', '正式观察输入', evidence.sourceMode === 'formal_observation'),
    gate('explicit_horizon', '预测期限明确', forecastHorizons.includes(evidence.horizon)),
    gate('model_manifest', '模型清单已绑定', evidence.modelManifestBound),
    gate('point_in_time', 'PIT 与修订链已验证', evidence.pointInTimeVerified),
    gate('frozen_oos', '冻结样本外回执', evidence.frozenOosReceiptBound),
    gate('calibration', '独立校准证明通过', evidence.calibrationProofAccepted),
    gate('effective_samples', `有效独立样本不少于 ${MIN_CALIBRATION_EFFECTIVE_SAMPLES}`, evidence.effectiveIndependentSamples >= MIN_CALIBRATION_EFFECTIVE_SAMPLES),
    gate('interval_coverage', '区间覆盖率已验证', evidence.intervalCoverageVerified),
    gate('cost_policy', '费用与滑点口径已绑定', evidence.costPolicyBound),
  ]
  const allPassed = gates.every((item) => item.passed)
  const illustrative = evidence.sourceMode === 'demo_fixture'
  return {
    status: allPassed ? 'decision_support_ready' : illustrative ? 'illustrative_only' : 'blocked',
    usableFor: allPassed ? 'manual_decision_support' : illustrative ? 'visual_illustration' : 'none',
    horizon: evidence.horizon,
    modelId: evidence.modelId,
    gates,
    probabilitiesVisible: allPassed,
    intervalsMayUseCoverageLabels: allPassed,
  }
}

function gate(id: ForecastReadinessGate['id'], label: string, passed: boolean): ForecastReadinessGate {
  return { id, label, passed }
}
