import type { PerformancePoint } from '../../types/dashboard'

export function ChartAccessibleSummary({ latest }: { latest: PerformancePoint | null }) {
  if (!latest) {
    return <p className="sr-only" aria-label="收益曲线摘要">历史曲线尚未形成</p>
  }

  return (
    <p className="sr-only" aria-label="收益曲线摘要">
      当前收益 {formatPercent(latest.simulated)}，目标 {formatPercent(latest.target)}，市场基准 {formatPercent(latest.benchmark)}。
    </p>
  )
}

function formatPercent(value: number) {
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}
