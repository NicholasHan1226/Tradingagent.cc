import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import { formatCurrency } from '../../lib/format'
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
  const gapLabel = targetGap >= 0 ? `高于目标 +${targetGap.toFixed(2)}%` : `低于目标 ${targetGap.toFixed(2)}%`
  const modeLabel = isLive ? '实盘' : '模拟盘'
  const hasAmount = portfolio !== null
  const primaryResult = hasAmount ? formatCurrency(liveProfit) : `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
  const resultCaption = hasAmount
    ? `收益率 ${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
    : '等待金额'
  const activityLabel = portfolio
    ? `${portfolio.tradeCount} 次成交 · ${portfolio.pointCount} 个收益点`
    : `兑现 ${executedCount} · 推进 ${pendingCount} · 复盘 ${missedCount}`
  const drawdownDistance = portfolio ? Math.max(0, DRAWDOWN_LIMIT_PCT - Math.abs(portfolio.maxDrawdownPct)) : null
  const drawdownLabel = drawdownDistance !== null ? `${drawdownDistance.toFixed(2)}%` : '等待'

  return (
    <aside className="realtime-return-card" aria-label="实时收益">
      <div className="return-card-head">
        <span>{modeLabel}</span>
        <div className="return-mode-switch" aria-label="账户层切换" role="tablist">
          <button className={!isLive ? 'selected' : ''} onClick={() => selectAccountMode('simulated')} type="button">
            模拟盘
          </button>
          <button className={isLive ? 'selected' : ''} onClick={() => selectAccountMode('live')} type="button">
            实盘
          </button>
        </div>
      </div>
      <span className="return-kicker">实时收益</span>
      {isLive ? (
        <div className="return-placeholder">
          <strong>实盘结果未开启</strong>
          <p>接入完成后可在这里切换查看；当前只展示模拟盘。</p>
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
          <div className="return-subline">
            <b>{modeLabel}</b>
            <em>{gapLabel}</em>
          </div>
          <div className="return-facts" aria-label="收益关键指标">
            <span>
              <em>目标差</em>
              <b>{targetGap >= 0 ? '+' : ''}{targetGap.toFixed(2)}%</b>
            </span>
            <span>
              <em>风险距离</em>
              <b>{drawdownLabel}</b>
            </span>
            <span>
              <em>成交</em>
              <b>{portfolio?.tradeCount ?? executedCount}</b>
            </span>
          </div>
          <small>{gapLabel} · {activityLabel}</small>
        </>
      )}
      <button onClick={() => setActivePage('收益')} type="button">查看收益详情</button>
    </aside>
  )
}
