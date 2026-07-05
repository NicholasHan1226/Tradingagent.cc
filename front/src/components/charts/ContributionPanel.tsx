import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { chartColors } from './chartConfig'

export function ContributionPanel({ signals }: { signals: SignalRow[] }) {
  const contributionData = getContributionData(signals)

  return (
    <section className="panel rail-panel">
      <PanelTitle kicker="赚钱原因" title="哪类判断贡献最大" />
      <div className="bar-box">
        <ResponsiveContainer width="100%" height={178}>
          <BarChart data={contributionData} layout="vertical" margin={{ top: 4, right: 18, bottom: 0, left: 24 }}>
            <CartesianGrid stroke={chartColors.grid} horizontal={false} />
            <XAxis hide type="number" />
            <YAxis dataKey="name" tick={{ fill: 'var(--text-muted)', fontSize: 11 }} tickLine={false} axisLine={false} type="category" />
            <Tooltip content={<ContributionTooltip />} cursor={{ fill: 'rgba(240, 246, 244, 0.035)' }} />
            <Bar dataKey="value" radius={[3, 3, 3, 3]}>
              {contributionData.map((entry) => (
                <Cell fill={entry.value < 0 ? chartColors.opportunity : chartColors.simulated} key={entry.name} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
  )
}

function getContributionData(signals: SignalRow[]) {
  const byMethod = signals.reduce<Record<string, number>>((acc, signal) => {
    const method = signal.method || '其他'
    acc[method] = (acc[method] ?? 0) + readImpact(signal.impact)
    return acc
  }, {})
  const rows = Object.entries(byMethod)
    .map(([name, value]) => ({ name, value: Number(value.toFixed(1)) }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
    .slice(0, 5)

  return rows.length ? rows : [{ name: '等待结果', value: 0 }]
}

function readImpact(value: string) {
  const parsed = Number(value.replace('+', '').replace('%', '').trim())
  return Number.isFinite(parsed) ? parsed : 0
}

export function ContributionTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      <span>{Number(payload[0].value).toFixed(1)}K</span>
    </div>
  )
}
