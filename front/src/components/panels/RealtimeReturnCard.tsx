import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import { formatCnyCompact, formatCurrency, formatSignedCnyCompact } from '../../lib/format'
import type { AccountMode, Page, PortfolioSummary } from '../../types/dashboard'

export function RealtimeReturnCard({
  accountMode,
  executedCount,
  hasPerformanceData,
  headline,
  liveProfit,
  liveReturn,
  missedCount,
  pendingCount,
  portfolio,
  selectAccountMode,
  setActivePage,
  targetReturn,
}: {
  accountMode: AccountMode
  executedCount: number
  hasPerformanceData: boolean
  headline: string
  liveProfit: number
  liveReturn: number
  missedCount: number
  pendingCount: number
  portfolio: PortfolioSummary | null
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  targetReturn: number
}) {
  const targetGap = liveReturn - targetReturn
  const isLive = accountMode === 'live'
  const ashareAccount = portfolio?.ashareAccount
  const isCnyPortfolio = portfolio?.pnlCurrency === 'CNY'
  const hasAmount = portfolio !== null
  const primaryResult = hasAmount
    ? isCnyPortfolio
      ? formatSignedCnyCompact(liveProfit)
      : formatCurrency(liveProfit)
    : `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
  const resultCaption = hasAmount
    ? `${formatSignedPct(liveReturn)} · 目标差 ${formatSignedPct(targetGap)}`
    : `${formatSignedPct(liveReturn)} · 等待金额`
  const activityLabel = portfolio
    ? `${portfolio.tradeCount} 次成交 · ${portfolio.pointCount} 个收益点`
    : `兑现 ${executedCount} · 推进 ${pendingCount} · 复盘 ${missedCount}`
  const accountLine = ashareAccount
    ? `总资产 ${formatCnyCompact(ashareAccount.accountEquity)} · 现金 ${formatCnyCompact(ashareAccount.cashAvailable)} · 持仓 ${formatCnyCompact(ashareAccount.marketValue)}`
    : portfolio
      ? `组合资金 ${formatCapital(portfolio)} · 成交 ${portfolio.tradeCount} · 收益点 ${portfolio.pointCount}`
      : activityLabel
  const strategyPnl = ashareAccount?.strategyTotalPnl
  const validSampleLabel = ashareAccount
    ? ashareAccount.totalSampleCount > 0
      ? `${ashareAccount.strategySampleValidCount}/${ashareAccount.totalSampleCount}`
      : '等待复盘'
    : `${portfolio?.tradeCount ?? executedCount}`
  const strategyLabel = ashareAccount?.strategySampleValidCount === 0
    ? '暂无复盘'
    : strategyPnl === undefined
      ? validSampleLabel
      : formatCnyCompact(strategyPnl)
  const drawdownDistance = portfolio ? Math.max(0, DRAWDOWN_LIMIT_PCT - Math.abs(portfolio.maxDrawdownPct)) : null
  const drawdownLabel = drawdownDistance !== null ? `${drawdownDistance.toFixed(2)}%` : '等待'

  return (
    <aside className="realtime-return-card" aria-label="实时收益">
      <div className="return-card-head">
        <span>{isLive ? '实盘预留' : '模拟盘最新结果'}</span>
        <div className="return-mode-switch" aria-label="账户层切换" role="tablist">
          <button
            aria-selected={!isLive}
            className={!isLive ? 'selected' : ''}
            onClick={() => selectAccountMode('simulated')}
            role="tab"
            type="button"
          >
            模拟盘
          </button>
          <button
            aria-selected={isLive}
            className={isLive ? 'selected' : ''}
            onClick={() => selectAccountMode('live')}
            role="tab"
            type="button"
          >
            实盘
          </button>
        </div>
      </div>
      <span className="return-kicker">{isLive ? '待接入' : '最新快照'} · 金额为主，收益率同步显示</span>
      {isLive ? (
        <div className="return-placeholder">
          <strong>实盘待接入</strong>
          <p>完成授权、风控和回执验证前，不展示真实资金结果。</p>
        </div>
      ) : !hasPerformanceData ? (
        <div className="return-placeholder">
          <strong>等待收益结果</strong>
          <p>{headline}</p>
        </div>
      ) : (
        <>
          <div className="return-primary">
            <strong>{primaryResult}</strong>
            <span>{resultCaption}</span>
          </div>
          <div className="return-facts" aria-label="收益关键指标">
            <span>
              <em>目标差</em>
              <b>{formatSignedPct(targetGap)}</b>
            </span>
            <span>
              <em>风险距离</em>
              <b>{drawdownLabel}</b>
            </span>
            <span>
              <em>{ashareAccount ? '可复盘' : '成交'}</em>
              <b>{validSampleLabel}</b>
            </span>
          </div>
          {ashareAccount && (
            <div
              className="return-account-grid"
              aria-label="A股模拟账户"
              title="可复盘记录会进入收益分析；历史验证记录只用于确认账户链路。"
            >
              <span>
                <em>总资产</em>
                <b>{formatCnyCompact(ashareAccount.accountEquity)}</b>
              </span>
              <span>
                <em>现金</em>
                <b>{formatCnyCompact(ashareAccount.cashAvailable)}</b>
              </span>
              <span>
                <em>持仓</em>
                <b>{formatCnyCompact(ashareAccount.marketValue)}</b>
              </span>
              <span>
                <em>账户盈亏</em>
                <b>{formatSignedCnyCompact(ashareAccount.accountTotalPnl)}</b>
              </span>
              <span>
                <em>复盘收益</em>
                <b>{strategyLabel}</b>
              </span>
              <span>
                <em>持仓数</em>
                <b>{ashareAccount.openPositionCount}</b>
              </span>
            </div>
          )}
          <small>{accountLine}</small>
        </>
      )}
      <button onClick={() => setActivePage('收益')} type="button">收益详情</button>
    </aside>
  )
}

function formatSignedPct(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : value
  return `${cleanValue > 0 ? '+' : ''}${cleanValue.toFixed(2)}%`
}

function formatCapital(portfolio: PortfolioSummary) {
  return portfolio.pnlCurrency === 'CNY' ? formatCnyCompact(portfolio.capitalBase) : formatCurrency(portfolio.capitalBase)
}
