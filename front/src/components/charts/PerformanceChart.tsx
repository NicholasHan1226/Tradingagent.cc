import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ChartEvent, Page, PerformancePoint } from '../../types/dashboard'
import { DRAWDOWN_LIMIT_PCT, TARGET_RETURN_PCT } from '../../lib/dashboardConstants'
import { chartColors } from './chartConfig'

function getPerformanceDomain(data: PerformancePoint[]) {
  const values = data.flatMap((point) => [point.simulated, point.target, point.benchmark, point.opportunity, -DRAWDOWN_LIMIT_PCT, TARGET_RETURN_PCT])
  const min = Math.floor(Math.min(...values) - 2)
  const max = Math.ceil(Math.max(...values) + 2)
  return [min, max] as [number, number]
}

export function PerformanceChart({
  data,
  events = [],
  height,
  latestPoint,
  onSelectEvent,
}: {
  data: PerformancePoint[]
  events?: ChartEvent[]
  height: number
  latestPoint: PerformancePoint
  onSelectEvent?: (page: Page) => void
}) {
  const targetGap = latestPoint.simulated - latestPoint.target
  const riskDistance = DRAWDOWN_LIMIT_PCT - Math.abs(Math.min(0, latestPoint.opportunity))
  const eventPoints = events
    .map((event) => {
      const point = data.find((item) => item.day === event.day)
      return point ? { event, point } : null
    })
    .filter((item): item is { event: ChartEvent; point: PerformancePoint } => Boolean(item))

  return (
    <div className="chart-box hyper-chart-panel" style={{ height }}>
      <div className="chart-panel-head">
        <div>
          <span>收益走势</span>
          <strong>{latestPoint.simulated >= 0 ? '+' : ''}{latestPoint.simulated.toFixed(2)}%</strong>
        </div>
        <div>
          <span>目标差</span>
          <strong>{targetGap >= 0 ? '+' : ''}{targetGap.toFixed(2)}%</strong>
        </div>
        <div>
          <span>风险距离</span>
          <strong>{Math.max(0, riskDistance).toFixed(2)}%</strong>
        </div>
      </div>
      <div className="chart-plot">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 10, right: 18, left: -8, bottom: 4 }}>
            <CartesianGrid stroke={chartColors.grid} vertical={false} />
            <XAxis
              dataKey="day"
              interval={4}
              tick={{ fill: 'var(--text-faint)', fontSize: 10 }}
              tickLine={false}
              axisLine={{ stroke: chartColors.axis }}
            />
            <YAxis
              width={42}
              domain={getPerformanceDomain(data)}
              tickCount={4}
              tickFormatter={(value) => `${value}%`}
              tick={{ fill: 'var(--text-faint)', fontSize: 10 }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: chartColors.cursor }} />
            <ReferenceLine y={TARGET_RETURN_PCT} stroke={chartColors.target} strokeDasharray="4 8" />
            <ReferenceLine y={-DRAWDOWN_LIMIT_PCT} stroke={chartColors.opportunity} strokeDasharray="4 8" />
            <ReferenceLine x="现在" stroke={chartColors.simulated} strokeDasharray="2 8" />
            <ReferenceDot
              x="现在"
              y={latestPoint.simulated}
              r={4}
              fill={chartColors.simulated}
              stroke="#050b0b"
              strokeWidth={2}
            />
            {eventPoints.map(({ event, point }) => (
              <ReferenceDot
                key={`${event.day}-${event.targetPage}-dot`}
                x={event.day}
                y={point.simulated}
                r={3.25}
                fill="#050b0b"
                stroke={chartColors.simulated}
                strokeWidth={1.4}
              />
            ))}
            <Line type="monotone" dataKey="simulated" stroke={chartColors.simulated} strokeWidth={2.15} dot={false} animationDuration={450} />
            <Line type="monotone" dataKey="target" stroke={chartColors.target} strokeWidth={1.25} strokeDasharray="7 8" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="benchmark" stroke={chartColors.benchmark} strokeWidth={1.15} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="opportunity" stroke={chartColors.opportunity} strokeWidth={1.2} strokeDasharray="7 8" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="chart-legend">
        <span><i className="cyan" />模拟盘</span>
        <span><i className="amber" />目标</span>
        <span><i className="muted" />市场基准</span>
        <span><i className="red" />机会差</span>
      </div>
      <div className="chart-live-labels" aria-hidden="true">
        <span className="current">现在 +{latestPoint.simulated.toFixed(2)}%</span>
      </div>
      {eventPoints.length > 0 && (
        <div className="chart-event-bar" aria-label="收益关键节点">
          {eventPoints.map(({ event }) => (
            <button
              aria-label={`查看 ${event.day} ${event.targetPage}`}
              key={`${event.day}-${event.targetPage}-button`}
              onClick={() => onSelectEvent?.(event.targetPage)}
              type="button"
            >
              <span>{event.day}</span>
              <strong>{event.title}</strong>
              <em>{event.summary}</em>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null

  const labels: Record<string, string> = {
    simulated: '模拟盘',
    target: '目标',
    benchmark: '市场基准',
    opportunity: '机会偏差',
  }

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      {payload.map((item: any) => (
        <span key={item.dataKey}>
          {labels[item.dataKey] ?? item.dataKey}: {Number(item.value).toFixed(2)}%
        </span>
      ))}
      <em>目标线 +{TARGET_RETURN_PCT.toFixed(2)}% · 风险线 -{DRAWDOWN_LIMIT_PCT.toFixed(2)}%</em>
    </div>
  )
}
