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
}: {
  accountMode: AccountMode
  executedCount: number
  liveProfit: number
  liveReturn: number
  missedCount: number
  pendingCount: number
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
}) {
  const targetGap = liveReturn - 8
  const isLive = accountMode === 'live'

  return (
    <aside className="realtime-return-card" aria-label="实时收益">
      <div className="return-card-head">
        <span>实时收益</span>
        <div className="return-mode-switch" aria-label="收益账户切换">
          <button className={!isLive ? 'selected' : ''} onClick={() => selectAccountMode('simulated')} type="button">
            模拟盘
          </button>
          <button className={isLive ? 'selected' : ''} onClick={() => selectAccountMode('live')} type="button">
            实盘预留
          </button>
        </div>
      </div>
      {isLive ? (
        <div className="return-placeholder">
          <strong>实盘尚未接入</strong>
          <p>真实资金接入前只展示准备状态，不展示模拟收益。</p>
        </div>
      ) : (
        <>
          <span className="return-kicker">模拟盘净收益</span>
          <strong>+{formatCurrency(liveProfit)}</strong>
          <div className="return-subline">
            <b>+{liveReturn.toFixed(2)}%</b>
            <em>目标差 +{targetGap.toFixed(2)}%</em>
          </div>
          <small>{executedCount} 个已兑现 · {pendingCount} 个观察中 · {missedCount} 个未兑现</small>
        </>
      )}
      <button onClick={() => setActivePage('收益')} type="button">查看收益原因</button>
    </aside>
  )
}
