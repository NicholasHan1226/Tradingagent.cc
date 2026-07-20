import { marketLabels } from '../../data/dashboard'
import { formatSignedCnyCompact, formatSignedUsdt, formatUsdt } from '../../lib/format'
import type { Market, MarketSummary } from '../../types/dashboard'

export function MarketSummaryPanel({
  activeMarket,
  summary,
}: {
  activeMarket: Market
  summary?: MarketSummary
}) {
  if (activeMarket === 'All Markets') return null

  const hasReturn = Boolean(
    summary &&
    summary.status !== 'empty' &&
    (summary.tradeCount > 0 ||
      (summary.filledCount ?? 0) > 0 ||
      Math.abs(summary.pnlAmount ?? 0) > 0.005 ||
      Math.abs(summary.returnPct ?? 0) > 0.005),
  )
  const runtimeState = summary?.runtimeState ?? (summary?.status === 'ready' ? 'normal' : summary?.status === 'partial' ? 'strategy_wait' : 'empty')
  const status = formatRuntimeState(runtimeState)

  return (
    <section className="rail-panel market-summary-panel" aria-label="当前市场摘要">
      <div className="rail-title">
        <span>当前市场</span>
        <strong>{marketLabels[activeMarket]}</strong>
      </div>
      <div className={`market-summary-status ${summary?.status ?? 'empty'} ${runtimeState}`}>
        <span>{status}</span>
        <strong>{summary ? formatHeadline(summary, activeMarket) : `${marketLabels[activeMarket]}暂无模拟记录`}</strong>
        <em>{summary ? formatDetail(summary) : '等待该市场写入模拟成交、持仓或收益。'}</em>
      </div>
      <div className="market-summary-grid">
        <SummaryMetric label="信号" value={String(summary?.signalCount ?? 0)} />
        <SummaryMetric label="持仓" value={String(summary?.holdingCount ?? 0)} />
        <SummaryMetric label="成交" value={String(summary?.tradeCount ?? 0)} />
        <SummaryMetric label="模式" value={formatStyleValue(summary)} />
      </div>
      <div className="market-summary-return">
        <span>当前收益</span>
        <strong>{hasReturn && summary ? formatSummaryReturn(summary) : '等待收益'}</strong>
      </div>
      {summary?.market === 'A-share' && summary.noTradeEvidence ? (
        <AshareCapitalTrace summary={summary} />
      ) : null}
      {summary?.market === 'CNFutures' && summary.cnFuturesReplayEvidence ? (
        <CNFuturesReplayTrace summary={summary} />
      ) : null}
    </section>
  )
}

function formatRuntimeState(state: MarketSummary['runtimeState']) {
  if (state === 'normal') return '运行中'
  if (state === 'strategy_wait') return '自动等待'
  if (state === 'needs_attention') return '运行异常'
  return '等待数据'
}

function formatHeadline(summary: MarketSummary, activeMarket: Market) {
  if (summary.runtimeState === 'strategy_wait') return `${marketLabels[activeMarket]}正在等更好的入场条件`
  if (summary.runtimeState === 'needs_attention' || summary.executionFault) return `${marketLabels[activeMarket]}存在运行异常`
  if (summary.holdingCount > 0) return `${marketLabels[activeMarket]}已有 ${summary.holdingCount} 个仓位`
  if (summary.tradeCount > 0) return `${marketLabels[activeMarket]}已有 ${summary.tradeCount} 次模拟成交`
  return `${marketLabels[activeMarket]}暂无新机会`
}

function formatDetail(summary: MarketSummary) {
  const parts = [
    summary.pnlAmount === undefined ? null : `收益 ${formatMarketMoney(summary.pnlAmount, summary.pnlCurrency, true)}`,
    summary.returnPct === undefined ? null : `收益率 ${summary.returnPct >= 0 ? '+' : ''}${summary.returnPct.toFixed(2)}%`,
    summary.capitalBase === undefined ? null : `资金 ${formatMarketMoney(summary.capitalBase, summary.pnlCurrency, false)}`,
    summary.activeStyleCount === undefined ? null : `模式 ${summary.activeStyleCount}/${summary.styleCount}`,
    summary.tradeCount ? `成交 ${summary.tradeCount}` : null,
  ].filter(Boolean)

  if (parts.length) return parts.join(' · ')
  if (summary.runtimeReason?.includes('waiting')) return '没有符合条件的新入场，继续观察。'
  return '等待该市场写入可展示结果。'
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
    : formatMarketMoney(summary.pnlAmount, summary.pnlCurrency, true)
  return [amountPart, returnPart].filter(Boolean).join(' · ')
}

function formatMarketMoney(value: number, currency: MarketSummary['pnlCurrency'], signed: boolean) {
  if (currency === 'CNY') return signed ? formatSignedCnyCompact(value) : formatSignedCnyCompact(value).replace('+', '')
  if (currency === 'USDT') return signed ? formatSignedUsdt(value) : formatUsdt(value)
  return '--'
}

function AshareCapitalTrace({ summary }: { summary: MarketSummary }) {
  const evidence = summary.noTradeEvidence
  if (!evidence) return null
  const rows = [
    evidence.strategyCashAvailable === undefined ? null : ['可用资金', formatSignedCnyCompact(evidence.strategyCashAvailable).replace('+', '')],
    evidence.accountCashAvailable === undefined ? null : ['账户现金', formatSignedCnyCompact(evidence.accountCashAvailable).replace('+', '')],
    evidence.strategyPositionCount === undefined && evidence.accountPositionCount === undefined
      ? null
      : ['复盘/账户持仓', `${evidence.strategyPositionCount ?? 0}/${evidence.accountPositionCount ?? 0}`],
    evidence.ignoredValidationSampleCount === undefined
      ? null
      : ['不计入复盘', `${evidence.ignoredValidationSampleCount}`],
  ].filter((row): row is [string, string] => Boolean(row))
  if (!rows.length) return null
  return (
    <div className="ashare-capital-trace" aria-label="A股资金状态">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  )
}

function CNFuturesReplayTrace({ summary }: { summary: MarketSummary }) {
  const evidence = summary.cnFuturesReplayEvidence
  if (!evidence) return null
  const rows: Array<[string, string]> = [
    ['回放窗口', `${evidence.windowCount}`],
    ['可执行/候选', `${evidence.executableCount}/${evidence.actionableCount}`],
    ['合约/模式', `${evidence.symbolCount}/${evidence.styleCount}`],
    evidence.nonExecutableReason ? ['未执行原因', evidence.nonExecutableReason] : evidence.topReason ? ['主要原因', evidence.topReason] : ['状态', evidence.actionableCount > 0 ? '有候选' : '继续等待'],
  ]
  return (
    <div className="ashare-capital-trace" aria-label="中国期货回放状态">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  )
}
