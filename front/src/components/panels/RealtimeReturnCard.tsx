import { formatCurrency } from '../../lib/format'
import type { AccountMode, Page } from '../../types/dashboard'

export function RealtimeReturnCard({
  accountMode,
  executedCount,
  hasPerformanceData,
  headline,
  liveProfit,
  liveReturn,
  missedCount,
  pendingCount,
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
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  targetReturn: number
}) {
  const targetGap = liveReturn - targetReturn
  const isLive = accountMode === 'live'
  const gapLabel = targetGap >= 0 ? `高于目标 +${targetGap.toFixed(2)}%` : `低于目标 ${targetGap.toFixed(2)}%`

  return (
    <aside className="realtime-return-card" aria-label="实时收益">
      <div className="return-card-head">
        <span>实时收益</span>
        <div className="return-mode-switch" aria-label="收益账户切换">
          <button className={!isLive ? 'selected' : ''} onClick={() => selectAccountMode('simulated')} type="button">
            模拟盘
          </button>
          <button className={isLive ? 'selected' : ''} onClick={() => selectAccountMode('live')} type="button">
            实盘
          </button>
        </div>
      </div>
      {isLive ? (
        <div className="return-placeholder">
          <strong>实盘未启用</strong>
          <p>授权和风控开关完成后，这里切换到真实账户结果。</p>
        </div>
      ) : !hasPerformanceData ? (
        <div className="return-placeholder">
          <strong>等待收益数据</strong>
          <p>{headline}</p>
        </div>
      ) : (
        <>
          <span className="return-kicker">模拟盘 · 当前收益</span>
          <strong>+{formatCurrency(liveProfit)}</strong>
          <div className="return-subline">
            <b>+{liveReturn.toFixed(2)}%</b>
            <em>{gapLabel}</em>
          </div>
          <small>{headline} {executedCount} 个兑现 · {pendingCount} 个推进 · {missedCount} 个复盘。</small>
        </>
      )}
      <button onClick={() => setActivePage('收益')} type="button">收益归因</button>
    </aside>
  )
}
