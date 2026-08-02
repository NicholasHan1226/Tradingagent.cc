export const forecastHorizons = ['m30', 'm60', 'close', '1d', '3d', '5d'] as const

export type ForecastHorizon = typeof forecastHorizons[number]
export type ForecastModelId = 'naive_last_value' | 'linear_ridge_baseline' | 'kronos_challenger'
export type ForecastReadinessStatus = 'illustrative_only' | 'decision_support_ready' | 'blocked'

export type ForecastEvidence = {
  sourceMode: 'demo_fixture' | 'formal_observation'
  horizon: ForecastHorizon
  modelId: ForecastModelId
  modelManifestBound: boolean
  modelManifestId: string | null
  modelManifestSha256: string | null
  pointInTimeVerified: boolean
  pointInTimeReceiptId: string | null
  pointInTimeReceiptSha256: string | null
  frozenOosReceiptBound: boolean
  frozenOosReceiptId: string | null
  frozenOosReceiptSha256: string | null
  calibrationProofAccepted: boolean
  calibrationReceiptId: string | null
  calibrationReceiptSha256: string | null
  effectiveIndependentSamples: number
  intervalCoverageVerified: boolean
  intervalCoverageReceiptId: string | null
  intervalCoverageReceiptSha256: string | null
  costPolicyBound: boolean
  costPolicyId: string | null
  costPolicySha256: string | null
  baselineComparisonAccepted: boolean
  baselineComparisonReceiptId: string | null
  baselineComparisonReceiptSha256: string | null
  postCostUtilityPositive: boolean
}

export type ForecastReadinessGate = {
  id: 'formal_observation' | 'explicit_horizon' | 'model_manifest' | 'point_in_time' | 'frozen_oos'
    | 'calibration' | 'effective_samples' | 'interval_coverage' | 'cost_policy' | 'baseline_comparison' | 'post_cost_utility'
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
  const formal = evidence.sourceMode === 'formal_observation'
  const gates: ForecastReadinessGate[] = [
    gate('formal_observation', '正式观察输入', formal),
    gate('explicit_horizon', '预测期限明确', forecastHorizons.includes(evidence.horizon)),
    gate('model_manifest', '模型清单及哈希已绑定', evidence.modelManifestBound && receiptBinding(evidence.modelManifestId, evidence.modelManifestSha256, formal)),
    gate('point_in_time', 'PIT 与修订链回执已验证', evidence.pointInTimeVerified && receiptBinding(evidence.pointInTimeReceiptId, evidence.pointInTimeReceiptSha256, formal)),
    gate('frozen_oos', '冻结样本外回执', evidence.frozenOosReceiptBound && receiptBinding(evidence.frozenOosReceiptId, evidence.frozenOosReceiptSha256, formal)),
    gate('calibration', '独立校准回执通过', evidence.calibrationProofAccepted && receiptBinding(evidence.calibrationReceiptId, evidence.calibrationReceiptSha256, formal)),
    gate('effective_samples', `有效独立样本不少于 ${MIN_CALIBRATION_EFFECTIVE_SAMPLES}`, evidence.effectiveIndependentSamples >= MIN_CALIBRATION_EFFECTIVE_SAMPLES),
    gate('interval_coverage', '区间覆盖率回执已验证', evidence.intervalCoverageVerified && receiptBinding(evidence.intervalCoverageReceiptId, evidence.intervalCoverageReceiptSha256, formal)),
    gate('cost_policy', '费用与滑点口径已绑定', evidence.costPolicyBound && receiptBinding(evidence.costPolicyId, evidence.costPolicySha256, formal)),
    gate('baseline_comparison', '同一 PIT 基线对照已通过', evidence.baselineComparisonAccepted && receiptBinding(evidence.baselineComparisonReceiptId, evidence.baselineComparisonReceiptSha256, formal)),
    gate('post_cost_utility', '扣除成本后净效用为正', evidence.postCostUtilityPositive),
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

function receiptBinding(id: string | null, sha256: string | null, required: boolean) {
  if (!required) return true
  return Boolean(id?.trim() && sha256 && /^[a-f0-9]{64}$/.test(sha256))
}
