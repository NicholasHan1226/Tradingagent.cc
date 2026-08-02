import type { SignalRow } from '../types/dashboard'
import type { CopilotAnalysis } from './types'

export function analysisFromSignal(signal: SignalRow, generatedAt: string): CopilotAnalysis {
  const blocked = ['blocked', 'cancelled', 'missed'].includes(signal.status)
  const sourceRefs = signal.capitalEvidence?.source ? [signal.capitalEvidence.source] : []
  const verdict = blocked ? '暂不参与' : '等待条件'
  return {
    symbol: signal.symbol,
    name: signal.name,
    mode: 'tradingagent_observation',
    generatedAt,
    evidenceStrength: {
      value: null,
      label: '上游观察未评分',
      semantics: 'unavailable',
      contractVersion: 'v1',
      sourceRefs,
      asOf: generatedAt,
    },
    readiness: {
      data: 'unavailable',
      evidence: 'unscored_observation',
      model: 'not_applicable',
      action: blocked ? 'blocked' : 'observe_only',
      reasons: [
        '通用信号 confidence 没有证据强度语义，禁止转换为 Copilot 分数',
        '等待带 receipt、PIT 时点和反证的正式个股投影',
      ],
    },
    verdict,
    summary: `TradingAgent 只读观察：${signal.reason || '当前没有补充原因'}。状态为${signal.status}，仍须由你复核。`,
    support: [
      { title: signal.method || '系统观察', detail: signal.reason || '当前观察未提供更多支持证据。', knownAt: generatedAt },
      ...(signal.capitalEvidence?.source ? [{ title: '量化证据来源', detail: signal.capitalEvidence.source, sourceRef: signal.capitalEvidence.source, knownAt: generatedAt }] : []),
    ],
    oppose: [
      { title: '反证完整度', detail: '当前快照没有结构化 bear-case；Copilot 因此不会把单向信号当成确定买入结论。', knownAt: generatedAt },
      ...(blocked ? [{ title: '当前系统状态', detail: `该观察已处于 ${signal.status}，不应形成新增交易计划。` }] : []),
    ],
    buyConditions: [signal.next || '等待下一步系统条件', '快照来源与时间仍然有效', '个人资金、持仓和止损风险复核通过'],
    invalidation: ['快照过期或来源降级', '系统状态变为 blocked / cancelled / missed', '个人组合风险超出计划'],
  }
}
