import { formatCnyCompact, formatSignedCnyCompact } from '../../lib/format'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'
import type { AShareTierSummary, Market } from '../../types/dashboard'

export function AShareTierComparisonPanel({
  activeMarket,
  summaries,
}: {
  activeMarket: Market
  summaries?: AShareTierSummary[]
}) {
  if (activeMarket !== 'A-share') return null
  if (!summaries || summaries.length < 2) return null

  return (
    <section className="panel rail-panel ashare-tier-comparison-panel" aria-label="A股资金档位对比">
      <PanelTitle kicker="A股实验" title="资金档位对比" />
      <div className="summary-list">
        {summaries.map((tier) => (
          <SummaryRow
            key={tier.account}
            label={tier.label}
            tone={tierTone(tier.totalPnl)}
            value={`${formatSignedCnyCompact(tier.totalPnl)} · ${formatReturnPct(tier.returnPct)}`}
          />
        ))}
      </div>
      <div className="ashare-tier-capital-grid" aria-label="档位资金">
        {summaries.map((tier) => (
          <div key={`${tier.account}-capital`}>
            <span>{tier.label}</span>
            <strong>{formatCnyCompact(tier.capital)}</strong>
            <em>{`${tier.tradeCount} 笔`}</em>
          </div>
        ))}
      </div>
      <small className="panel-footnote">同一策略在不同本金与手数约束下的模拟结果</small>
    </section>
  )
}

function tierTone(totalPnl: number) {
  if (totalPnl > 0.005) return 'cyan'
  if (totalPnl < -0.005) return 'red'
  return 'muted'
}

function formatReturnPct(returnPct: number) {
  const cleanValue = Math.abs(returnPct) < 0.005 ? 0 : returnPct
  return `${cleanValue > 0 ? '+' : ''}${cleanValue.toFixed(2)}%`
}
