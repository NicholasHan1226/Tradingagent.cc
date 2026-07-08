import { formatSignedCnyCompact } from '../../lib/format'
import type { Market, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function AShareMoneyflowPanel({
  activeMarket,
  signals,
}: {
  activeMarket: Market
  signals: SignalRow[]
}) {
  if (activeMarket !== 'All Markets' && activeMarket !== 'A-share') return null

  const rows = signals
    .filter((signal) => signal.market === 'A-share' && signal.capitalEvidence)
    .sort((left, right) => (right.capitalEvidence?.score ?? Number.NEGATIVE_INFINITY) - (left.capitalEvidence?.score ?? Number.NEGATIVE_INFINITY))
    .slice(0, 5)

  return (
    <section className="panel rail-panel ashare-moneyflow-panel" aria-label="A股资金流">
      <PanelTitle kicker="A股资金" title="个股流向" />
      {rows.length ? (
        <div className="moneyflow-list">
          {rows.map((signal) => (
            <div className="moneyflow-row" key={`${signal.symbol}-${signal.method}`}>
              <div>
                <strong>{signal.symbol}</strong>
                <span>{formatScore(signal.capitalEvidence?.score)}</span>
              </div>
              <em>{formatFlow(signal.capitalEvidence?.netInflow)}</em>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-panel-copy compact-copy">
          <strong>暂无资金流信号</strong>
          <span>A股机会带有资金流数据后，会显示净流入和强弱。</span>
        </div>
      )}
    </section>
  )
}

function formatScore(score?: number) {
  if (score === undefined) return '资金分 --'
  const normalized = score <= 1 ? score * 100 : score
  return `资金分 ${Math.round(normalized)}`
}

function formatFlow(value?: number) {
  if (value === undefined) return '净流入 --'
  return `净流入 ${formatSignedCnyCompact(value)}`
}
