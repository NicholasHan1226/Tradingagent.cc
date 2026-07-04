import { OutcomePill } from '../OutcomePill'
import { SummaryRow } from '../SummaryRow'

export function DecisionFormation() {
  const rows = [
    { label: '发现机会', value: 1284, detail: '全市场都看过' },
    { label: '理由清楚', value: 843, detail: '值得继续看' },
    { label: '可以下手', value: 612, detail: '条件已满足' },
    { label: '风险可控', value: 487, detail: '风险可接受' },
    { label: '进入组合', value: 356, detail: '仓位已安排' },
    { label: '形成结果', value: 329, detail: '已兑现或已处理' },
  ]
  const maxValue = rows[0].value

  return (
    <div className="formation-flow">
      <div className="formation-header">
        <span>漏斗留存</span>
        <strong>1,284 → 329</strong>
      </div>
      <div className="formation-funnel" aria-label="决策形成漏斗">
        {rows.map((row, index) => (
          <FunnelRow key={row.label} maxValue={maxValue} previousValue={rows[index - 1]?.value} row={row} />
        ))}
      </div>
      <div className="formation-outcomes">
        <OutcomePill label="已兑现 59.3%" tone="cyan" value="195" />
        <OutcomePill label="观察中 23.4%" tone="amber" value="77" />
        <OutcomePill label="已保护 9.4%" tone="red" value="31" />
        <OutcomePill label="已放弃 7.9%" tone="muted" value="26" />
      </div>
      <div className="formation-notes">
        <SummaryRow label="研究把握度" value="78%" />
        <SummaryRow label="平均耗时" value="18分钟" />
        <SummaryRow label="兑现率" value="72.4%" tone="cyan" />
        <SummaryRow label="避开风险" value="$1.24M" tone="cyan" />
      </div>
    </div>
  )
}

function FunnelRow({
  maxValue,
  previousValue,
  row,
}: {
  maxValue: number
  previousValue?: number
  row: { label: string; value: number; detail: string }
}) {
  const retain = (row.value / maxValue) * 100
  const drop = previousValue ? ((previousValue - row.value) / previousValue) * 100 : 0

  return (
    <div className="funnel-row">
      <div className="funnel-copy">
        <span>{row.label}</span>
        <strong>{row.value.toLocaleString('en-US')}</strong>
        <em>{row.detail}</em>
      </div>
      <div className="funnel-meter" aria-hidden="true">
        <i style={{ width: `${retain}%` }} />
      </div>
      <b>{previousValue ? `流失 ${drop.toFixed(1)}%` : '基准'}</b>
    </div>
  )
}
