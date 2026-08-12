import type { StockIntelligence } from './stockIntelligence'
import type { CopilotAnalysis } from './types'

export type CoverageStatus = 'formal_fresh' | 'observation_only' | 'coverage_gap'

export type CoverageStatusOutcome = {
  status: CoverageStatus
  label: '正式且新鲜' | '仅观察' | '覆盖缺口'
  detail: string
}

const coverageGap: CoverageStatusOutcome = {
  status: 'coverage_gap',
  label: '覆盖缺口',
  detail: '没有可用于该股票的已验证正式投影或只读观察。',
}

export function resolveCoverageStatus({ analysis, intelligence }: {
  analysis?: CopilotAnalysis
  intelligence?: StockIntelligence
}): CoverageStatusOutcome {
  if (intelligence) {
    const source = intelligence.source
    const formalObservation = intelligence.mode === 'tradingagent_observation'
      && intelligence.verification.status === 'verified'
      && source !== null

    if (formalObservation && source?.freshness === 'fresh' && source.activityAuthorityStatus?.quality !== 'usable_degraded') {
      return {
        status: 'formal_fresh',
        label: '正式且新鲜',
        detail: '已验证正式投影，且其来源明确标记为 fresh。',
      }
    }

    if (formalObservation) {
      return {
        status: 'observation_only',
        label: '仅观察',
        detail: source?.activityAuthorityStatus?.quality === 'usable_degraded'
          ? `正式事实投影可读，但会话 authority 缺口：${source.activityAuthorityStatus.missingFields.join('、')}`
          : '存在只读观察，但不具备已验证的 fresh 正式投影。',
      }
    }

    return coverageGap
  }

  if (analysis?.mode === 'tradingagent_observation') {
    return {
      status: 'observation_only',
      label: '仅观察',
      detail: '存在只读观察，但不具备已验证的 fresh 正式投影。',
    }
  }

  return coverageGap
}
