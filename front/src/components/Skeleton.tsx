type SkeletonProps = {
  rows?: number
  height?: number
}

export function ChartSkeleton({ height = 280 }: SkeletonProps) {
  return (
    <div className="skeleton-chart" style={{ minHeight: height }} aria-label="正在准备图表">
      <span />
      <span />
      <span />
      <span />
    </div>
  )
}

export function TableSkeleton({ rows = 5 }: SkeletonProps) {
  return (
    <div className="skeleton-table" aria-label="正在准备列表">
      {Array.from({ length: rows }).map((_, index) => (
        <span key={index} />
      ))}
    </div>
  )
}
