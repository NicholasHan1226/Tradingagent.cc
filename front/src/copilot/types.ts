export type CopilotDecisionAction = 'planned' | 'observing' | 'skipped'
export type CopilotAnalysisMode = 'tradingagent_observation' | 'demo_fixture' | 'analysis_unavailable'

export type CopilotHolding = {
  symbol: string
  name: string
  quantity: number
  sellableQuantity: number
  averageCost: number
  updatedAt: string
}

export type CopilotWatchItem = {
  symbol: string
  name: string
  addedAt: string
}

export type CopilotDecision = {
  id: string
  symbol: string
  action: CopilotDecisionAction
  recordedAt: string
  actor: string
  authority: 'human_intent_only'
  plan?: {
    reason: string
    trigger: string
    invalidation: string
    maxRiskCny: number | null
  }
  review?: {
    status: 'pending' | 'executed' | 'not_executed' | 'expired'
    actualAction: string
    note: string
    reviewedAt: string
  } | null
}

export type TradingCopilotState = {
  schemaVersion: 1
  ownerId: string
  source: 'user_declared'
  updatedAt: string
  account: {
    declaredCapitalCny: number
    availableCashCny: number
    updatedAt: string
  }
  holdings: CopilotHolding[]
  watchlist: CopilotWatchItem[]
  decisions: CopilotDecision[]
}

export type CopilotEvidence = {
  title: string
  detail: string
  sourceRef?: string
  knownAt?: string
}

export type CopilotEvidenceStrength = {
  value: number | null
  label: string
  semantics: 'typed_evidence_strength_v1' | 'demo_unscored' | 'unavailable'
  contractVersion: 'v1'
  sourceRefs: string[]
  asOf: string | null
}

export type CopilotDecisionReadiness = {
  data: 'verified' | 'demo' | 'unavailable'
  evidence: 'typed' | 'unscored_observation' | 'demo' | 'unavailable'
  model: 'ready' | 'blocked' | 'not_applicable'
  action: 'eligible_for_human_review' | 'observe_only' | 'blocked'
  reasons: string[]
}

export type CopilotAnalysis = {
  symbol: string
  name: string
  mode: CopilotAnalysisMode
  generatedAt: string | null
  evidenceStrength: CopilotEvidenceStrength
  readiness: CopilotDecisionReadiness
  verdict: '积极观察' | '等待条件' | '暂不参与' | '暂无分析'
  summary: string
  support: CopilotEvidence[]
  oppose: CopilotEvidence[]
  buyConditions: string[]
  invalidation: string[]
}

export function emptyTradingCopilotState(now = new Date().toISOString()): TradingCopilotState {
  return {
    schemaVersion: 1,
    ownerId: 'nicholas',
    source: 'user_declared',
    updatedAt: now,
    account: { declaredCapitalCny: 0, availableCashCny: 0, updatedAt: now },
    holdings: [],
    watchlist: [],
    decisions: [],
  }
}

export function isAshareSymbol(value: string) {
  return /^[0-9]{6}\.(SH|SZ)$/.test(value.trim().toUpperCase())
}
