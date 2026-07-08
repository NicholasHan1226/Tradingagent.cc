import { getHomeOutcome } from '../../lib/dashboard'
import { formatCurrency, formatSignedCnyCompact } from '../../lib/format'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function HomeResultBrief({
  hasSignalData,
  portfolio,
  holdings,
  setActivePage,
  signals,
}: {
  hasHoldingData: boolean
  hasPerformanceData: boolean
  hasSignalData: boolean
  holdings: HoldingRow[]
  portfolio: PortfolioSummary | null
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const { blockedSignal, leadSignal, leadingHolding, reviewSignal } = getHomeOutcome(signals, holdings)
  const targetGap = portfolio ? portfolio.returnPct - portfolio.targetPct : null
  const leadingHoldingText = leadingHolding ? `${leadingHolding.symbol} · ${leadingHolding.pnl}` : '暂无持仓'
  const resultTone = portfolio && portfolio.pnlAmount < 0 ? '承压' : portfolio && portfolio.pnlAmount > 0 ? '领先' : '平稳'
  const resultLine = portfolio
    ? `${formatAmount(portfolio)} / ${formatSignedPct(portfolio.returnPct)}`
    : '等待收益'
  const attentionLine = hasSignalData
    ? leadSignal?.symbol ?? reviewSignal?.symbol ?? '有信号待查看'
    : holdings.length
      ? leadingHoldingText
      : '暂无新机会'
  const riskLine = portfolio
    ? `距离 7% 限制 ${Math.max(0, 7 - Math.abs(portfolio.maxDrawdownPct)).toFixed(2)}%`
    : '等待风控结果'

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="看机会" kicker="首页摘要" onAction={() => setActivePage('机会')} title="现在要看" />
      <div className="home-brief-score">
        <span>{resultTone}</span>
        <strong>{resultLine}</strong>
        <em>{targetGap === null ? '收益写入后自动更新' : `目标差 ${formatSignedPct(targetGap)}`}</em>
      </div>
      <div className="home-brief-facts">
        <span>
          <em>当前焦点</em>
          <b>{attentionLine}</b>
        </span>
        <span>
          <em>风险边界</em>
          <b>{riskLine}</b>
        </span>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">需要看的三件事</span>
        <div className="decision-list compact">
          {hasSignalData && leadSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('机会')} type="button">
              <span>优先跟进</span>
              <strong>{leadSignal.symbol}</strong>
              <em>{leadSignal.reason}</em>
            </button>
          )}
          {hasSignalData && !leadSignal && leadingHolding && (
            <button className="decision-mini-row" onClick={() => setActivePage('持仓')} type="button">
              <span>继续观察</span>
              <strong>{leadingHolding.symbol}</strong>
              <em>{leadingHolding.name}</em>
            </button>
          )}
          {!hasSignalData && holdings.length > 0 && (
            <button className="decision-mini-row" onClick={() => setActivePage('持仓')} type="button">
              <span>先看持仓</span>
              <strong>{leadingHolding?.symbol ?? `${holdings.length} 个仓位`}</strong>
              <em>新机会为空，当前结果主要来自已有仓位。</em>
            </button>
          )}
          {hasSignalData && blockedSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('风险')} type="button">
              <span>先不要追</span>
              <strong>{blockedSignal.symbol}</strong>
              <em>{blockedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && reviewSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('复盘')} type="button">
              <span>回头检查</span>
              <strong>{reviewSignal.symbol}</strong>
              <em>{reviewSignal.reason}</em>
            </button>
          )}
          {!hasSignalData && holdings.length === 0 && (
            <div className="empty-panel-copy compact-copy">
              <strong>暂无机会结果</strong>
              <span>新机会进入后，首页只保留最需要看的结果。</span>
            </div>
          )}
        </div>
      </div>
      <button className="secondary-action" onClick={() => setActivePage('收益')} type="button">
        看完整收益
      </button>
    </section>
  )
}

function formatSignedPct(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : value
  return `${cleanValue > 0 ? '+' : ''}${cleanValue.toFixed(2)}%`
}

function formatAmount(portfolio: PortfolioSummary) {
  return portfolio.pnlCurrency === 'CNY'
    ? formatSignedCnyCompact(portfolio.pnlAmount)
    : formatCurrency(portfolio.pnlAmount)
}
