import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { marketLabels } from '../../data/dashboard'
import type { HoldingRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { ContributionTooltip } from './ContributionPanel'
import { pieColors } from './chartConfig'

export function AllocationPanel({ holdings }: { holdings: HoldingRow[] }) {
  const allocationData = getAllocationData(holdings)

  return (
    <section className="panel rail-panel">
      <PanelTitle kicker="资金去向" title="现在投在哪里" />
      <div className="pie-box">
        <ResponsiveContainer width="100%" height={210}>
          <PieChart>
            <Pie data={allocationData} dataKey="value" innerRadius={52} outerRadius={82} paddingAngle={1}>
              {allocationData.map((entry, index) => (
                <Cell fill={pieColors[index % pieColors.length]} key={entry.name} />
              ))}
            </Pie>
            <Tooltip content={<ContributionTooltip />} />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="allocation-list">
        {allocationData.map((item, index) => (
          <span key={item.name}>
            <i style={{ background: pieColors[index % pieColors.length] }} />
            {item.name}
            <b>{item.value}%</b>
          </span>
        ))}
      </div>
    </section>
  )
}

function getAllocationData(holdings: HoldingRow[]) {
  const byMarket = holdings.reduce<Record<string, number>>((acc, holding) => {
    const market = marketLabels[holding.market]
    acc[market] = (acc[market] ?? 0) + readPercent(holding.weight)
    return acc
  }, {})

  const rows = Object.entries(byMarket)
    .map(([name, value]) => ({ name, value: Number(value.toFixed(1)) }))
    .sort((a, b) => b.value - a.value)

  return rows.length ? rows : [{ name: '等待持仓', value: 100 }]
}

function readPercent(value: string) {
  const parsed = Number(value.replace('%', '').trim())
  return Number.isFinite(parsed) ? parsed : 0
}
