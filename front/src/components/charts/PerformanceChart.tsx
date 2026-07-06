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

function getFocusedPerformanceDomain(data: PerformancePoint[], latestPoint: PerformancePoint) {
  const rawDomain = getPerformanceDomain(data)
  const visibleValues = [latestPoint.simulated, latestPoint.target, latestPoint.benchmark, latestPoint.opportunity, -DRAWDOWN_LIMIT_PCT, TARGET_RETURN_PCT]
  const center = (Math.min(...visibleValues) + Math.max(...visibleValues)) / 2
  const span = Math.max(18, Math.min(52, Math.abs(latestPoint.simulated - latestPoint.target) + 20))
  const min = Math.floor(Math.max(rawDomain[0], center - span / 2))
  const max = Math.ceil(Math.min(rawDomain[1], center + span / 2))

  return max - min >= 14 ? [min, max] as [number, number] : rawDomain
}

function projectIntoDomain(value: number, [min, max]: [number, number]) {
  const padding = Math.max(1.5, (max - min) * 0.05)
  if (value > max) return max - padding + Math.tanh((value - max) / 40) * padding
  if (value < min) return min + padding - Math.tanh((min - value) / 40) * padding
  return value
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
  const visualDomain = getFocusedPerformanceDomain(data, latestPoint)
  const plotData = data.map((point) => ({
    ...point,
    simulatedPlot: projectIntoDomain(point.simulated, visualDomain),
    targetPlot: projectIntoDomain(point.target, visualDomain),
    benchmarkPlot: projectIntoDomain(point.benchmark, visualDomain),
    opportunityPlot: projectIntoDomain(point.opportunity, visualDomain),
  }))
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
          <LineChart data={plotData} margin={{ top: 10, right: 18, left: -8, bottom: 4 }}>
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
              allowDataOverflow
              domain={visualDomain}
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
              y={projectIntoDomain(latestPoint.simulated, visualDomain)}
              r={4}
              fill={chartColors.simulated}
              stroke="#050b0b"
              strokeWidth={2}
            />
            {eventPoints.map(({ event, point }) => (
              <ReferenceDot
                key={`${event.day}-${event.targetPage}-dot`}
                x={event.day}
                y={projectIntoDomain(point.simulated, visualDomain)}
                r={3.25}
                fill="#050b0b"
                stroke={chartColors.simulated}
                strokeWidth={1.4}
              />
            ))}
            <Line type="monotone" dataKey="simulatedPlot" stroke={chartColors.simulated} strokeWidth={2.15} dot={false} animationDuration={450} />
            <Line type="monotone" dataKey="targetPlot" stroke={chartColors.target} strokeWidth={1.25} strokeDasharray="7 8" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="benchmarkPlot" stroke={chartColors.benchmark} strokeWidth={1.15} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="opportunityPlot" stroke={chartColors.opportunity} strokeWidth={1.2} strokeDasharray="7 8" dot={false} isAnimationActive={false} />
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
    simulatedPlot: '模拟盘',
    targetPlot: '目标',
    benchmarkPlot: '市场基准',
    opportunityPlot: '机会差',
  }

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      {payload.map((item: any) => (
        <span key={item.dataKey}>
          {labels[item.dataKey] ?? item.dataKey}: {Number(item.payload?.[String(item.dataKey).replace('Plot', '')] ?? item.value).toFixed(2)}%
        </span>
      ))}
      <em>视图按当前区间压缩；数值为真实记录。</em>
    </div>
  )
}
