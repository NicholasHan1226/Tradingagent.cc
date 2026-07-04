import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { chartColors } from './chartConfig'

const riskData = [
  { day: '5月6日', drawdown: -1.4, saved: 0.1 },
  { day: '5月14日', drawdown: -2.2, saved: 0.3 },
  { day: '5月22日', drawdown: -3.1, saved: 0.5 },
  { day: '5月30日', drawdown: -4.8, saved: 0.8 },
  { day: '6月7日', drawdown: -5.5, saved: 1.1 },
  { day: '6月15日', drawdown: -6.4, saved: 1.2 },
  { day: '现在', drawdown: -6.12, saved: 1.24 },
]

export function RiskTimeline() {
  return (
    <div className="risk-timeline">
      <ResponsiveContainer width="100%" height={360}>
        <AreaChart data={riskData} margin={{ top: 22, right: 36, left: 4, bottom: 8 }}>
          <CartesianGrid stroke={chartColors.grid} />
          <XAxis dataKey="day" tick={{ fill: 'var(--text-faint)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fill: 'var(--text-faint)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip content={<RiskTooltip />} cursor={{ stroke: chartColors.cursor }} />
          <ReferenceLine y={-7} stroke={chartColors.opportunity} strokeDasharray="6 6" />
          <Area type="monotone" dataKey="drawdown" stroke={chartColors.opportunity} fill="rgba(238, 99, 107, 0.08)" strokeWidth={2} />
          <Area type="monotone" dataKey="saved" stroke={chartColors.simulated} fill="rgba(94, 234, 223, 0.08)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
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
