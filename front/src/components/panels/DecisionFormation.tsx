import { getSignalFunnel } from '../../lib/dashboard'
import type { PortfolioSummary, SignalRow } from '../../types/dashboard'
import { OutcomePill } from '../OutcomePill'
import { SummaryRow } from '../SummaryRow'

export function DecisionFormation({ portfolio, signals }: { portfolio: PortfolioSummary | null; signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const rows = [
    { label: '发现', value: funnel.stages[0]?.rows.length ?? 0, detail: '全市场扫描' },
    { label: '研究', value: funnel.stages[1]?.rows.length ?? 0, detail: '证据自动归集' },
    { label: '风控', value: funnel.stages[2]?.rows.length ?? 0, detail: '约束自动校验' },
    { label: '模拟执行', value: funnel.stages[3]?.rows.length ?? 0, detail: '进入受控执行' },
    { label: '结果写回', value: funnel.stages[4]?.rows.length ?? 0, detail: '结果完成归档' },
  ]
  const maxValue = Math.max(1, rows[0].value)
  const averageLatency = getAverageLatency(signals)

  return (
    <div className="formation-flow">
      <div className="formation-header">
        <span>过程完成</span>
        <strong>{rows[0].value.toLocaleString('en-US')} → {rows[rows.length - 1].value.toLocaleString('en-US')}</strong>
      </div>
      <div className="formation-funnel" aria-label="自动过程完成漏斗">
        {rows.map((row, index) => (
          <FunnelRow key={row.label} maxValue={maxValue} previousValue={rows[index - 1]?.value} row={row} />
        ))}
      </div>
      <div className="formation-outcomes">
        <OutcomePill label={`${ratio(funnel.executed.length, signals.length)} 已写回`} tone="cyan" value={String(funnel.executed.length)} />
        <OutcomePill label={`${ratio(funnel.pending.length, signals.length)} 运行中`} tone="amber" value={String(funnel.pending.length)} />
        <OutcomePill label={`${ratio(funnel.blocked.length, signals.length)} 安全拦截`} tone="red" value={String(funnel.blocked.length)} />
        <OutcomePill label={`${ratio(funnel.cancelled.length, signals.length)} 自动终止`} tone="muted" value={String(funnel.cancelled.length)} />
      </div>
      <div className="formation-notes">
        <SummaryRow label="进入执行" value={ratio(funnel.tradeSignals.length, signals.length)} />
        <SummaryRow label="平均耗时" value={averageLatency ? `${averageLatency}分钟` : '等待记录'} />
        <SummaryRow label="完成率" value={ratio(funnel.executed.length, Math.max(1, funnel.tradeSignals.length))} tone="cyan" />
        <SummaryRow label="当前收益" value={portfolio ? `${portfolio.returnPct >= 0 ? '+' : ''}${portfolio.returnPct.toFixed(2)}%` : '等待收益'} tone={portfolio && portfolio.returnPct >= 0 ? 'cyan' : undefined} />
      </div>
    </div>
  )
}

function ratio(value: number, total: number) {
  return `${Math.round((value / Math.max(1, total)) * 100)}%`
}

function getAverageLatency(signals: SignalRow[]) {
  const latencies = signals.map((signal) => signal.stageLatencyMinutes).filter((value): value is number => typeof value === 'number' && Number.isFinite(value) && value > 0)
  if (!latencies.length) return 0
  return Math.round(latencies.reduce((total, value) => total + value, 0) / latencies.length)
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
      <b>{previousValue ? `未通过 ${drop.toFixed(1)}%` : '起点'}</b>
    </div>
  )
}
