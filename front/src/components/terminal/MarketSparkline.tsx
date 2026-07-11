export function MarketSparkline({ label, points, tone }: { label: string; points: number[]; tone: 'positive' | 'negative' | 'flat' }) {
  if (points.length < 2) return <span className="market-sparkline-empty">单点</span>
  const width = 64
  const height = 20
  const min = Math.min(...points)
  const max = Math.max(...points)
  const span = Math.max(max - min, Math.abs(max) * 0.001, 0.0001)
  const coordinates = points.map((value, index) => {
    const x = (index / (points.length - 1)) * width
    const y = height - 2 - ((value - min) / span) * (height - 4)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  return (
    <svg aria-label={`${label} 价格走势，${points.length} 个真实数据点`} className={`market-sparkline ${tone}`} role="img" viewBox={`0 0 ${width} ${height}`}>
      <polyline fill="none" points={coordinates} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
