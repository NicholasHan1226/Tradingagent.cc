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

function getFocusedPerformanceDomain(data: PerformancePoint[], latestPoint: PerformancePoint) {
  const visiblePoints = data.filter((point) => point.quality !== 'outlier')
  const domainPoints = visiblePoints.length >= 3 ? visiblePoints : data
  const visibleValues = domainPoints.flatMap((point) => [
    point.simulated,
    point.target,
    point.benchmark,
    point.opportunity,
  ])
  visibleValues.push(latestPoint.simulated, latestPoint.target, latestPoint.benchmark, latestPoint.opportunity, -DRAWDOWN_LIMIT_PCT, TARGET_RETURN_PCT)

  const baseMin = Math.min(...visibleValues)
  const baseMax = Math.max(...visibleValues)
  const baseSpan = Math.max(18, baseMax - baseMin)
  const min = Math.floor(baseMin - Math.max(4, baseSpan * 0.16))
  const max = Math.ceil(Math.max(baseMax + Math.max(6, baseSpan * 0.28), latestPoint.simulated + Math.max(9, Math.abs(latestPoint.simulated - latestPoint.target) * 0.65)))

  return [min, max] as [number, number]
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
  const hasOutlierSegment = data.some((point) => point.quality === 'outlier')
  const plotData = data.map((point) => ({
    ...point,
    simulatedPlot: projectIntoDomain(point.simulated, visualDomain),
    simulatedNormalPlot: point.quality === 'outlier' ? null : projectIntoDomain(point.simulated, visualDomain),
    simulatedOutlierPlot: point.quality === 'outlier' ? projectIntoDomain(point.simulated, visualDomain) : null,
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
                stroke={point.quality === 'outlier' ? chartColors.target : chartColors.simulated}
                strokeWidth={1.4}
              />
            ))}
            <Line type="monotone" dataKey="simulatedNormalPlot" stroke={chartColors.simulated} strokeWidth={2.15} dot={false} animationDuration={450} />
            <Line
              type="monotone"
              dataKey="simulatedOutlierPlot"
              stroke={chartColors.target}
              strokeOpacity={0.46}
              strokeWidth={1}
              strokeDasharray="2 9"
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
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
        {hasOutlierSegment && <span><i className="outlier" />口径跳变</span>}
      </div>
      {hasOutlierSegment && <div className="chart-quality-note">异常区间已弱化</div>}
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
    simulatedNormalPlot: '模拟盘',
    simulatedOutlierPlot: '口径跳变',
    targetPlot: '目标',
    benchmarkPlot: '市场基准',
    opportunityPlot: '机会差',
  }

  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      {payload.map((item: any) => {
        const rawKey = item.dataKey === 'simulatedNormalPlot' || item.dataKey === 'simulatedOutlierPlot'
          ? 'simulated'
          : String(item.dataKey).replace('Plot', '')

        return (
          <span key={item.dataKey}>
            {labels[item.dataKey] ?? item.dataKey}: {Number(item.payload?.[rawKey] ?? item.value).toFixed(2)}%
          </span>
        )
      })}
      {payload.some((item: any) => item.payload?.quality === 'outlier') && <span>标记: {payload[0]?.payload?.qualityReason ?? '口径跳变候选'}</span>}
      <em>视图按当前区间压缩；数值为真实记录。</em>
    </div>
  )
}
