import type { ReactNode } from 'react'
import type {
  AShareMarketMaturityProjection,
  AShareSampleKpiProjection,
  CNFuturesMarketMaturityProjection,
  Market,
  MarketSummary,
} from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function MarketMaturityPanel({
  activeMarket,
  ashareMaturity,
  ashareSampleKpi,
  cnFuturesMaturity,
  marketSummaries,
}: {
  activeMarket: Market
  ashareMaturity?: AShareMarketMaturityProjection
  ashareSampleKpi?: AShareSampleKpiProjection
  cnFuturesMaturity?: CNFuturesMarketMaturityProjection
  marketSummaries: MarketSummary[]
}) {
  const showAshare = activeMarket === 'All Markets' || activeMarket === 'A-share'
  const showCNFutures = activeMarket === 'All Markets' || activeMarket === 'CNFutures'
  if (!showAshare && !showCNFutures) return null

  const ashareCapital = marketSummaries.find((summary) => summary.market === 'A-share')
  const cnFuturesCapital = marketSummaries.find((summary) => summary.market === 'CNFutures')

  return (
    <section className="panel rail-panel market-maturity-panel" aria-label="市场成熟度与样本证据">
      <PanelTitle kicker="样本闭环" title="成熟度与资金" />
      <p className="maturity-boundary">资金独立，不跨市场净额</p>
      {showAshare ? (
        <MarketEvidenceCard
          title="A股 5万模拟账户"
          stage={ashareMaturity?.stage}
          dayLabel={ashareMaturity ? `模拟第 ${ashareMaturity.totalTradingDays} 个交易日` : '等待交易日证据'}
          checkpoint={ashareCheckpointLabel(ashareMaturity)}
          capital={ashareCapital}
        >
          {ashareSampleKpi ? <AShareSampleEvidence sample={ashareSampleKpi} /> : <EmptyEvidence copy="等待 A股 SampleJournal KPI" />}
        </MarketEvidenceCard>
      ) : null}
      {showCNFutures ? (
        <MarketEvidenceCard
          title="期货 5万模拟账户"
          stage={cnFuturesMaturity?.stage}
          dayLabel={cnFuturesMaturity ? `模拟第 ${cnFuturesMaturity.totalSimulationTradingDays} 个交易日` : '等待交易日证据'}
          checkpoint="长期模拟成熟度"
          capital={cnFuturesCapital}
        >
          {cnFuturesMaturity ? <CNFuturesEvidence maturity={cnFuturesMaturity} /> : <EmptyEvidence copy="等待期货成熟度快照" />}
        </MarketEvidenceCard>
      ) : null}
      <footer>仅模拟 · 自动晋级关闭</footer>
    </section>
  )
}

function MarketEvidenceCard({
  capital,
  checkpoint,
  children,
  dayLabel,
  stage,
  title,
}: {
  capital?: MarketSummary
  checkpoint: string
  children: ReactNode
  dayLabel: string
  stage?: string
  title: string
}) {
  return (
    <article className="maturity-market-card">
      <header>
        <strong>{title}</strong>
        <span>{dayLabel}</span>
      </header>
      <div className="maturity-stage-row">
        <b>{checkpoint}</b>
        <em>{stage ?? 'evidence_pending'}</em>
      </div>
      <CapitalEvidence summary={capital} />
      {children}
    </article>
  )
}

function CapitalEvidence({ summary }: { summary?: MarketSummary }) {
  if (!summary?.capitalAuthorityId || summary.capitalBase !== 50_000) {
    return <EmptyEvidence copy="等待当前 5 万资金 authority" />
  }
  const utilization = summary.capitalUtilizationPct
  const riskUsed = summary.riskUsedCny
  const riskLimit = summary.riskLimitCny
  const reasons = summary.undeployedReasons ?? []
  return (
    <div className="maturity-capital" aria-label={`${summary.market}资金利用`}>
      <span>资金利用 {utilization === undefined ? '--' : `${utilization.toFixed(1)}%`}</span>
      <span>可预约 {formatCny(summary.availableToReserveCny)}</span>
      <strong>组合风险 {formatCny(riskUsed)} / {formatCny(riskLimit)}</strong>
      <small>{reasons.length ? `未部署：${reasons.map((reason) => reason.code).join('、')}` : '未部署原因：等待权威记录'}</small>
    </div>
  )
}

function AShareSampleEvidence({ sample }: { sample: AShareSampleKpiProjection }) {
  return (
    <div className="maturity-samples">
      <div className="maturity-metric-strip" aria-label="A股分层样本">
        <span>候选 {sample.candidateCount}</span>
        <span>预测 {sample.predictionCount}</span>
        <span>观察 {sample.observationCounterfactualCount}</span>
        <span>探索 {sample.explorationFillCount}</span>
        <span>利用 {sample.exploitationFillCount}</span>
        <span>完整回合 {sample.completedRoundTripCount}</span>
        <span>标签 {sample.readyForwardLabelCount}/{sample.pendingForwardLabelCount}</span>
        <span>拒绝 {sample.riskRejectCount}</span>
      </div>
      {sample.styles.length ? (
        <div className="maturity-style-list" aria-label="A股分风格样本">
          {sample.styles.map((style) => (
            <div key={style.styleId}>
              <strong>{style.styleId}</strong>
              <span>候选 {style.candidateCount} · 预测 {style.predictionCount}</span>
              <span>探索 {style.explorationFillCount} · 利用 {style.exploitationFillCount}</span>
              <span>回合 {style.completedRoundTripCount} · 标签 {style.readyForwardLabelCount}/{style.pendingForwardLabelCount}</span>
              <span>期望 {formatCny(style.expectancyCny)} · 费后 {formatCny(style.postCostPnlCny)}</span>
              <small>{style.rejectionReasons.length ? `拒绝：${style.rejectionReasons.map((row) => `${row.reason} ${row.count}`).join('、')}` : `拒绝 ${style.riskRejectCount}`}</small>
            </div>
          ))}
        </div>
      ) : <EmptyEvidence copy="等待分风格样本" />}
    </div>
  )
}

function CNFuturesEvidence({ maturity }: { maturity: CNFuturesMarketMaturityProjection }) {
  const counts = maturity.sampleCounts
  const coverage = maturity.coverage
  return (
    <div className="maturity-samples">
      <div className="maturity-metric-strip" aria-label="期货分层样本">
        <span>有效样本 {counts.validSampleCount}</span>
        <span>观察 {counts.observationCounterfactualCount}</span>
        <span>反事实 {counts.counterfactualOnlyCount}</span>
        <span>可执行 {counts.executionEligibleSampleCount}</span>
        <span>完整回合 {counts.completedRoundTripCount}</span>
        <span>标签 {counts.forwardLabelCount}/{counts.pendingForwardLabelCount}</span>
        <span>拒绝 {counts.riskRejectCount}</span>
      </div>
      <div className="maturity-coverage" aria-label="期货覆盖率">
        <span>品种 {coverage.productCount}</span>
        <span>波动状态 {coverage.volatilityRegimeCount}</span>
        <span>夜盘 {coverage.nightSessionSampleCount}</span>
        <span>换月 {coverage.rolloverSampleCount}</span>
        <span>保证金 {coverage.marginEvidenceSampleCount}</span>
        <span>费用 {coverage.feeEvidenceSampleCount}</span>
        <span>滑点 {coverage.slippageEvidenceSampleCount}</span>
        <span>极端行情 {coverage.extremeRiskSampleCount}</span>
      </div>
      <p className="maturity-blockers">
        {maturity.blockingReasons.length ? maturity.blockingReasons.join(' · ') : '当前无成熟度阻断'}
      </p>
    </div>
  )
}

function EmptyEvidence({ copy }: { copy: string }) {
  return <p className="maturity-empty">{copy}</p>
}

function ashareCheckpointLabel(maturity?: AShareMarketMaturityProjection) {
  if (!maturity) return 'Day 5 / Day 10 待证据'
  if (maturity.checkpointDue === 5 || maturity.totalTradingDays === 5) return 'Day 5 复核'
  if (maturity.checkpointDue === 10 || maturity.totalTradingDays >= 10) return 'Day 10 复核'
  if (maturity.totalTradingDays < 5) return `距 Day 5 复核 ${5 - maturity.totalTradingDays} 日`
  return `距 Day 10 复核 ${10 - maturity.totalTradingDays} 日`
}

function formatCny(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  return `¥${Math.round(value).toLocaleString('zh-CN')}`
}
