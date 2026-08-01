export type BaselineForecastPoint = {
  median: number
  narrowEnvelope: [number, number]
  wideEnvelope: [number, number]
}

export type BaselineForecast = {
  modelId: 'linear_ridge_baseline'
  trainingPointCount: number
  slopePerStep: number
  points: BaselineForecastPoint[]
}

export function buildLinearBaseline(prices: number[], steps: number): BaselineForecast | null {
  if (steps < 1 || prices.length < 8 || prices.some((value) => !Number.isFinite(value) || value <= 0)) return null
  const window = prices.slice(-Math.min(20, prices.length))
  const meanX = (window.length - 1) / 2
  const meanY = window.reduce((sum, value) => sum + value, 0) / window.length
  let numerator = 0
  let denominator = 0
  window.forEach((value, index) => {
    numerator += (index - meanX) * (value - meanY)
    denominator += (index - meanX) ** 2
  })
  const rawSlope = denominator ? numerator / denominator : 0
  const last = window.at(-1)!
  const cappedSlope = Math.max(-last * 0.015, Math.min(last * 0.015, rawSlope))
  const residuals = window.map((value, index) => Math.abs(value - (meanY + cappedSlope * (index - meanX))))
  const robustScale = Math.max(last * 0.002, median(residuals) * 1.4826)
  return {
    modelId: 'linear_ridge_baseline', trainingPointCount: window.length, slopePerStep: cappedSlope,
    points: Array.from({ length: steps }, (_, index) => {
      const step = index + 1
      const center = last + cappedSlope * step
      const scale = robustScale * Math.sqrt(step)
      return {
        median: round(center),
        narrowEnvelope: [round(center - scale * 0.65), round(center + scale * 0.65)],
        wideEnvelope: [round(center - scale * 1.3), round(center + scale * 1.3)],
      }
    }),
  }
}

function median(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2
}

function round(value: number) { return Math.round(value * 100) / 100 }
