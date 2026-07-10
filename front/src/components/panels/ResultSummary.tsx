import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function ResultSummary({
  holdings,
  portfolio,
  setActivePage,
  signals,
}: {
  holdings: HoldingRow[]
  portfolio: PortfolioSummary | null
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const topHolding = holdings[0]
  const missed = signals.filter((signal) => signal.status === 'missed')
  const blocked = signals.filter((signal) => signal.status === 'blocked')
  const returnValue = portfolio ? `${portfolio.returnPct >= 0 ? '+' : ''}${portfolio.returnPct.toFixed(2)}%` : '等待收益'

  return (
    <section className="panel rail-panel">
      <PanelTitle action="看过程" kicker="结果来源" onAction={() => setActivePage('过程')} title="当前结果" />
      <div className="summary-list">
        <SummaryRow label="当前收益" value={returnValue} tone={portfolio && portfolio.returnPct >= 0 ? 'cyan' : undefined} />
        <SummaryRow label="最大贡献" value={topHolding ? `${topHolding.symbol} ${topHolding.pnl}` : '等待持仓'} tone={topHolding?.pnl.startsWith('-') ? 'red' : 'cyan'} />
        <SummaryRow label="自动复盘" value={`${missed.length} 条`} tone={missed.length ? 'amber' : undefined} />
        <SummaryRow label="风险已挡住" value={`${blocked.length} 条`} tone={blocked.length ? 'red' : 'cyan'} />
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        查看收益贡献
      </button>
    </section>
  )
}
