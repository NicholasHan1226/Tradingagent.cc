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
import { chartColors } from './chartConfig'

function getPerformanceDomain(data: PerformancePoint[]) {
  const values = data.flatMap((point) => [point.simulated, point.target, point.benchmark, point.opportunity, -7, 8])
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
  const eventPoints = events
    .map((event) => {
      const point = data.find((item) => item.day === event.day)
      return point ? { event, point } : null
    })
    .filter((item): item is { event: ChartEvent; point: PerformancePoint } => Boolean(item))

  return (
    <div className="chart-box" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 26, right: 42, left: 0, bottom: 8 }}>
          <CartesianGrid stroke={chartColors.grid} />
          <XAxis
            dataKey="day"
            interval={3}
            tick={{ fill: 'var(--text-faint)', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            width={42}
            domain={getPerformanceDomain(data)}
            tickFormatter={(value) => `${value}%`}
            tick={{ fill: 'var(--text-faint)', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip content={<ChartTooltip />} cursor={{ stroke: chartColors.cursor }} />
          <ReferenceLine y={8} stroke={chartColors.target} strokeDasharray="4 7" />
          <ReferenceLine y={-7} stroke={chartColors.opportunity} strokeDasharray="4 7" />
          <ReferenceLine x="现在" stroke={chartColors.simulated} strokeDasharray="2 8" />
          <ReferenceDot
            x="现在"
            y={latestPoint.simulated}
            r={4}
            fill={chartColors.simulated}
            stroke="#081011"
            strokeWidth={2}
          />
          {eventPoints.map(({ event, point }) => (
            <ReferenceDot
              key={`${event.day}-${event.targetPage}-dot`}
              x={event.day}
              y={point.simulated}
              r={3.5}
              fill="var(--surface-panel)"
              stroke={chartColors.simulated}
              strokeWidth={1.6}
            />
          ))}
          <Line type="monotone" dataKey="simulated" stroke={chartColors.simulated} strokeWidth={2.25} dot={false} animationDuration={450} />
          <Line type="monotone" dataKey="target" stroke={chartColors.target} strokeWidth={1.45} strokeDasharray="7 7" dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="benchmark" stroke={chartColors.benchmark} strokeWidth={1.25} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="opportunity" stroke={chartColors.opportunity} strokeWidth={1.3} strokeDasharray="7 7" dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div className="chart-legend">
        <span><i className="cyan" />模拟盘</span>
        <span><i className="amber" />目标</span>
        <span><i className="muted" />市场基准</span>
        <span><i className="red" />机会缺口</span>
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
    opportunity: '机会缺口',
  }

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      {payload.map((item: any) => (
        <span key={item.dataKey}>
          {labels[item.dataKey] ?? item.dataKey}: {Number(item.value).toFixed(2)}%
        </span>
      ))}
      <em>目标线 +8.00% · 风险线 -7.00%</em>
    </div>
  )
}
