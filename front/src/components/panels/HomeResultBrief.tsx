import { getHomeOutcome } from '../../lib/dashboard'
import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import { formatCnyCompact, formatCurrency, formatSignedCnyCompact } from '../../lib/format'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function HomeResultBrief({
  hasHoldingData,
  hasPerformanceData,
  hasSignalData,
  holdings,
  portfolio,
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
  const drawdown = portfolio?.maxDrawdownPct ?? 0
  const drawdownLimit = DRAWDOWN_LIMIT_PCT
  const drawdownDistance = Math.max(0, drawdownLimit - Math.abs(drawdown))
  const drawdownCaption = drawdownDistance > 1 ? `距离 ${drawdownLimit}% 限制 ${drawdownDistance.toFixed(2)}%` : `接近 ${drawdownLimit}% 限制`
  const drawdownValue = hasPerformanceData ? `${Math.abs(drawdown).toFixed(2)}%` : '等待记录'
  const ashareAccount = portfolio?.ashareAccount
  const returnValue = portfolio
    ? `${ashareAccount ? formatSignedCnyCompact(portfolio.pnlAmount) : formatCurrency(portfolio.pnlAmount)} / ${portfolio.returnPct >= 0 ? '+' : ''}${portfolio.returnPct.toFixed(2)}%`
    : hasPerformanceData
      ? '有收益曲线'
      : '等待收益'
  const resultTone = hasPerformanceData && portfolio?.returnPct !== undefined && portfolio.returnPct >= 0 ? 'cyan' : undefined
  const mainSignalCount = hasSignalData ? `${signals.length}` : '0'
  const holdingLabel = hasHoldingData ? leadingHolding?.symbol ?? '有记录' : '暂无'
  const accountFact = ashareAccount
    ? `总资产 ${formatCnyCompact(ashareAccount.accountEquity)} · 现金 ${formatCnyCompact(ashareAccount.cashAvailable)} · 持仓 ${formatCnyCompact(ashareAccount.marketValue)}`
    : null
  const strategyFact = ashareAccount
    ? `可复盘 ${ashareAccount.strategySampleValidCount}/${ashareAccount.totalSampleCount} · 链路验证 ${ashareAccount.validationSampleCount}`
    : null

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="看机会" kicker="首页摘要" onAction={() => setActivePage('机会')} title="现在结果" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">一眼看清</span>
        <div className="brief-scorecard">
          <div className={`brief-scorecard-main ${resultTone ?? ''}`}>
            <span>收益</span>
            <strong>{returnValue}</strong>
            <em>{hasPerformanceData ? '金额和收益率合并展示' : '等待结果写入'}</em>
          </div>
          <div className="brief-mini-grid">
            <SummaryRow label="回撤" value={drawdownValue} tone={hasPerformanceData ? 'red' : undefined} />
            <SummaryRow label="机会" value={mainSignalCount} tone={hasSignalData ? 'cyan' : undefined} />
            <SummaryRow label="持仓" value={holdingLabel} />
          </div>
          {accountFact && (
            <div className="brief-risk-line">
              <span>账户事实</span>
              <strong>{accountFact}</strong>
            </div>
          )}
          {strategyFact && (
            <div
              className="brief-risk-line muted"
              title="可复盘样本可进入策略胜率、归因和自我进化；链路验证样本只确认历史成交闭环。"
            >
              <span>样本质量</span>
              <strong>{strategyFact}</strong>
            </div>
          )}
          <div className="brief-risk-line">
            <span>风险边界</span>
            <strong>{hasPerformanceData ? drawdownCaption : '等待记录'}</strong>
          </div>
          {portfolio?.pnlSource && (
            <div className="brief-risk-line muted">
              <span>计算方式</span>
              <strong>{formatPnlSource(portfolio.pnlSource)}</strong>
            </div>
          )}
        </div>
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
          {!hasSignalData && (
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

function formatPnlSource(value: string) {
  if (value === 'sim_ledger_mark_to_market') return '当前持仓估算'
  if (value === 'ashare_local_sim_account') return 'A股本地账本'
  if (value === 'ashare_local_sim_mark_to_market') return 'A股收盘盯市'
  if (value === 'ashare_local_sim_trade_price_fallback') return 'A股成交价估算'
  if (value === 'mixed') return '多来源汇总'
  return value
}
