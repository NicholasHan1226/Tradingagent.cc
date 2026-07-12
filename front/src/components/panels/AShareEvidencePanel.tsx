import type { AShareForwardValidation, AShareResearchEvidence } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function AShareEvidencePanel({ evidence, forwardValidation }: { evidence?: AShareResearchEvidence; forwardValidation?: AShareForwardValidation }) {
  if (!evidence) {
    return (
      <section className="panel rail-panel ashare-evidence-panel">
        <PanelTitle kicker="A股研究" title="开盘准备" />
        <div className="empty-panel-copy compact-copy">
          <strong>等待研究记录</strong>
          <span>集合竞价、尾盘观察和风格样本写入后会自动显示。</span>
        </div>
        {forwardValidation ? <ForwardValidationRows validation={forwardValidation} /> : null}
      </section>
    )
  }

  const openingLabel = formatOpeningState(evidence.openingAuction)
  const closingLabel = formatClosingState(evidence)
  const predictions = evidence.styleEvidence.summary.predictionCount ?? 0
  const fills = (evidence.styleEvidence.summary.explorationFillCount ?? 0)
    + (evidence.styleEvidence.summary.exploitationFillCount ?? 0)

  return (
    <section className="panel rail-panel ashare-evidence-panel">
      <PanelTitle kicker="A股研究" title="开盘准备" />
      <div className="summary-list">
        <SummaryRow label="集合竞价" value={openingLabel} tone={evidence.openingAuction.anomalyCount > 0 ? 'amber' : undefined} />
        <SummaryRow label="尾盘观察" value={closingLabel} tone={evidence.closingMomentum.candidateCount > 0 ? 'cyan' : undefined} />
        <SummaryRow label="逆回购" value={`${formatMoney(evidence.reverseRepo.amount)} / ${formatPercent(evidence.reverseRepo.annualizedYield)}`} />
        <SummaryRow label="风格样本" value={`${Math.trunc(predictions)} 预测 · ${Math.trunc(fills)} 成交`} tone="cyan" />
      </div>
      {forwardValidation ? <ForwardValidationRows validation={forwardValidation} /> : null}
      {evidence.closingMomentum.candidates.length > 0 && (
        <div className="ashare-evidence-list" aria-label="尾盘观察">
          {evidence.closingMomentum.candidates.slice(0, 3).map((candidate) => (
            <div key={candidate.symbol}>
              <span>{candidate.symbol}</span>
              <strong>{formatPercent(candidate.tailMomentum ?? 0)}</strong>
              <em>{formatLabelState(candidate.labelState)}</em>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ForwardValidationRows({ validation }: { validation: AShareForwardValidation }) {
  return (
    <div className="summary-list compact-summary-list" aria-label="A股成交验证">
      <SummaryRow label="成交验证" value={`${validation.strategyLabelCount}/${validation.tradeCount}`} tone={validation.strategyLabelCount > 0 ? 'cyan' : undefined} />
      <SummaryRow label="待确认" value={String(validation.pendingCount)} tone={validation.pendingCount > 0 ? 'amber' : undefined} />
    </div>
  )
}

function formatOpeningState(opening: AShareResearchEvidence['openingAuction']) {
  if (opening.anomalyCount > 0) return `${opening.anomalyCount} 个异动`
  if (opening.dataMode === 'first_5m_proxy') return `开盘样本 ${opening.proxySymbolsWithBars ?? 0}`
  if (opening.symbolsWithBars > 0) return '无明显异动'
  return '等待数据'
}

function formatClosingState(evidence: AShareResearchEvidence) {
  const count = evidence.closingMomentum.candidateCount
  if (count > 0) return `${count} 个观察`
  if (evidence.closingMomentum.symbolsWithBars > 0) return '暂无观察'
  return '等待5分钟线'
}

function formatLabelState(value?: string) {
  if (value === 'labeled') return '已兑现'
  if (value === 'pending_next_day_bar') return '待次日'
  return '观察'
}

function formatMoney(value: number) {
  return `¥${Math.round(value).toLocaleString('zh-CN')}`
}

function formatPercent(value: number) {
  return `${(value * 100).toFixed(2)}%`
}
