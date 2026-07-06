import { marketLabels } from '../../data/dashboard'
import { formatCurrency, formatSignedCnyCompact } from '../../lib/format'
import type { Market, MarketSummary } from '../../types/dashboard'

export function MarketSummaryPanel({
  activeMarket,
  summary,
}: {
  activeMarket: Market
  summary?: MarketSummary
}) {
  if (activeMarket === 'All Markets') return null

  const hasReturn = summary?.pnlAmount !== undefined || summary?.returnPct !== undefined
  const status = summary?.status === 'ready' ? '运行中' : summary?.status === 'partial' ? '部分记录' : '等待数据'

  return (
    <section className="rail-panel market-summary-panel" aria-label="当前市场摘要">
      <div className="rail-title">
        <span>当前市场</span>
        <strong>{marketLabels[activeMarket]}</strong>
      </div>
      <div className={`market-summary-status ${summary?.status ?? 'empty'}`}>
        <span>{status}</span>
        <strong>{summary?.headline ?? `${marketLabels[activeMarket]}暂无模拟记录`}</strong>
        <em>{summary?.detail ?? '等待该市场写入模拟成交、持仓或风格收益。'}</em>
      </div>
      <div className="market-summary-grid">
        <SummaryMetric label="信号" value={String(summary?.signalCount ?? 0)} />
        <SummaryMetric label="持仓" value={String(summary?.holdingCount ?? 0)} />
        <SummaryMetric label="成交" value={String(summary?.tradeCount ?? 0)} />
        <SummaryMetric label="风格" value={formatStyleValue(summary)} />
      </div>
      <div className="market-summary-return">
        <span>收益口径</span>
        <strong>{hasReturn ? formatSummaryReturn(summary!) : '等待收益'}</strong>
      </div>
    </section>
  )
}

function SummaryMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function formatStyleValue(summary?: MarketSummary) {
  if (!summary?.styleCount) return '0'
  if (summary.activeStyleCount === undefined) return String(summary.styleCount)
  return `${summary.activeStyleCount}/${summary.styleCount}`
}

function formatSummaryReturn(summary: MarketSummary) {
  const returnPart = summary.returnPct === undefined ? '' : `${summary.returnPct >= 0 ? '+' : ''}${summary.returnPct.toFixed(2)}%`
  const amountPart = summary.pnlAmount === undefined
    ? ''
    : summary.pnlCurrency === 'CNY'
      ? formatSignedCnyCompact(summary.pnlAmount)
      : formatCurrency(summary.pnlAmount)
  return [amountPart, returnPart].filter(Boolean).join(' · ')
}
