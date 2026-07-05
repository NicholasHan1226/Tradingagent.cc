import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import type { PerformancePoint, PortfolioSummary } from '../../types/dashboard'
import { chartColors } from './chartConfig'

export function RiskTimeline({ data, portfolio }: { data: PerformancePoint[]; portfolio: PortfolioSummary | null }) {
  const riskData = getRiskData(data, portfolio)

  return (
    <div className="risk-timeline">
      <ResponsiveContainer width="100%" height={360}>
        <AreaChart data={riskData} margin={{ top: 22, right: 36, left: 4, bottom: 8 }}>
          <CartesianGrid stroke={chartColors.grid} />
          <XAxis dataKey="day" tick={{ fill: 'var(--text-faint)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fill: 'var(--text-faint)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip content={<RiskTooltip />} cursor={{ stroke: chartColors.cursor }} />
          <ReferenceLine y={-DRAWDOWN_LIMIT_PCT} stroke={chartColors.opportunity} strokeDasharray="6 6" />
          <Area type="monotone" dataKey="drawdown" stroke={chartColors.opportunity} fill="rgba(238, 99, 107, 0.08)" strokeWidth={2} />
          <Area type="monotone" dataKey="saved" stroke={chartColors.simulated} fill="rgba(94, 234, 223, 0.08)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function getRiskData(data: PerformancePoint[], portfolio: PortfolioSummary | null) {
  if (!data.length) return [{ day: '等待', drawdown: 0, saved: 0 }]
  let peak = data[0]?.simulated ?? 0
  const rows = data.map((point) => {
    peak = Math.max(peak, point.simulated)
    const drawdown = Number(Math.min(0, point.simulated - peak).toFixed(2))
    return {
      day: point.day,
      drawdown,
      saved: Number(Math.abs(Math.min(0, point.opportunity)).toFixed(2)),
    }
  })

  if (portfolio && rows.length) {
    rows[rows.length - 1] = {
      ...rows[rows.length - 1],
      drawdown: -Math.abs(portfolio.maxDrawdownPct),
    }
  }

  return rows
}

function RiskTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      {payload.map((item: any) => (
        <span key={item.dataKey}>{item.dataKey === 'drawdown' ? '回撤' : '避险'}: {Number(item.value).toFixed(2)}</span>
      ))}
    </div>
  )
}
