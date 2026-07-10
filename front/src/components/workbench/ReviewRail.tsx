import type { ReactNode } from 'react'
import { marketLabels } from '../../data/dashboard'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'

export function ReviewRail({
  active,
  positions,
  portfolio,
  review,
  setActivePage,
}: {
  active: SignalRow[]
  positions: HoldingRow[]
  portfolio: PortfolioSummary | null
  review: SignalRow[]
  setActivePage: (page: Page) => void
}) {
  const reviewSignal = review[0]
  const activeSignal = active[0]
  const riskPosition = positions.find((position) => position.risk === '偏高' || position.risk === '观察')
  const position = riskPosition ?? positions[0]

  if (reviewSignal) {
    return (
      <RailFrame title="需要复盘" count={review.length} onOpen={() => setActivePage('机会')}>
        <span className="review-rail-kicker">{marketLabels[reviewSignal.market]} · {reviewSignal.symbol}</span>
        <strong>{reviewSignal.name}</strong>
        <p>{reviewSignal.reason}</p>
        <dl>
          <div><dt>状态</dt><dd>{reviewSignal.status === 'blocked' ? '风险拦截' : '结果待复盘'}</dd></div>
          <div><dt>下一步</dt><dd>{reviewSignal.next}</dd></div>
        </dl>
      </RailFrame>
    )
  }

  if (activeSignal) {
    return (
      <RailFrame title="正在推进" count={active.length} onOpen={() => setActivePage('机会')}>
        <span className="review-rail-kicker">{marketLabels[activeSignal.market]} · {activeSignal.symbol}</span>
        <strong>{activeSignal.name}</strong>
        <p>{activeSignal.reason}</p>
        <dl>
          <div><dt>还差什么</dt><dd>{activeSignal.next}</dd></div>
          <div><dt>有效期</dt><dd>{activeSignal.age}</dd></div>
        </dl>
      </RailFrame>
    )
  }

  if (position) {
    return (
      <RailFrame title={riskPosition ? '风险关注' : '持仓跟踪'} count={positions.length} onOpen={() => setActivePage('持仓')}>
        <span className="review-rail-kicker">{marketLabels[position.market]} · {position.symbol}</span>
        <strong>{position.name}</strong>
        <p>{position.role}</p>
        <dl>
          <div><dt>仓位</dt><dd>{position.weight}</dd></div>
          <div><dt>收益</dt><dd>{position.pnl}</dd></div>
          <div><dt>风险</dt><dd>{position.risk}</dd></div>
        </dl>
      </RailFrame>
    )
  }

  return (
    <RailFrame title="等待机会" count={0} onOpen={() => setActivePage('机会')}>
      <span className="review-rail-kicker">只读工作台</span>
      <strong>当前没有需要处理的记录</strong>
      <p>新机会、持仓风险或复盘事项出现后，会优先显示在这里。</p>
      {portfolio && <small>组合仍在记录：{portfolio.tradeCount} 次成交，{portfolio.pointCount} 个收益点。</small>}
    </RailFrame>
  )
}

function RailFrame({
  children,
  count,
  onOpen,
  title,
}: {
  children: ReactNode
  count: number
  onOpen: () => void
  title: string
}) {
  return (
    <aside className="workbench-review-rail" aria-label="当前审阅">
      <div className="review-rail-head">
        <div>
          <span>当前审阅</span>
          <h2>{title}</h2>
        </div>
        <b>{count}</b>
      </div>
      <div className="review-rail-body">{children}</div>
      <button onClick={onOpen} type="button">查看完整记录</button>
    </aside>
  )
}
