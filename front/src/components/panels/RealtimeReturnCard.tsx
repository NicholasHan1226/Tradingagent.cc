import { formatCurrency } from '../../lib/format'
import type { AccountMode, Page } from '../../types/dashboard'

export function RealtimeReturnCard({
  accountMode,
  executedCount,
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
          <strong>实盘待接入</strong>
          <p>接入完成后显示真实收益、持仓和风险。</p>
        </div>
      ) : (
        <>
          <span className="return-kicker">当前模拟盘收益</span>
          <strong>+{formatCurrency(liveProfit)}</strong>
          <div className="return-subline">
            <b>+{liveReturn.toFixed(2)}%</b>
            <em>{gapLabel}</em>
          </div>
          <small>{executedCount} 个兑现 · {pendingCount} 个推进 · {missedCount} 个错过时机</small>
        </>
      )}
      <button onClick={() => setActivePage('收益')} type="button">收益归因</button>
    </aside>
  )
}
