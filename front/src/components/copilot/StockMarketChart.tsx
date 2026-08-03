import { useId, useMemo } from 'react'
import {
  Area, Bar, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { ChartNoAxesCombined, Eye, EyeOff, SlidersHorizontal } from 'lucide-react'
import type { ForecastReadinessStatus } from '../../copilot/forecastReadiness'
import type { StockIntelligence, StockRange, StockSeriesPoint } from '../../copilot/stockIntelligence'

const rangeOptions: StockRange[] = ['1D', '5D', '1M', '6M', 'YTD', '1Y']

export function StockMarketChart({ intelligence, range, showForecast, onRangeChange, onToggleForecast }: {
  intelligence: StockIntelligence
  range: StockRange
  showForecast: boolean
  onRangeChange: (range: StockRange) => void
  onToggleForecast: () => void
}) {
  const summaryId = useId()
  const points = intelligence.series[range]
  const forecastMatchesRange = range === '1D' && intelligence.forecast?.horizon === 'm30'
  const forecastBlocked = intelligence.forecast?.readiness.usableFor === 'none'
  const forecastVisible = showForecast && forecastMatchesRange && !forecastBlocked
  const chartData = useMemo(() => forecastVisible ? points : points.filter((point) => point.price !== null), [points, forecastVisible])
  const lastHistorical = points.findLast((point) => point.price !== null)
  const isDemo = intelligence.mode === 'demo_fixture'
  const sourceLabel = isDemo ? '演示收盘' : intelligence.source ? `${intelligence.source.datasetId} · ${intelligence.source.freshness}` : '来源未验证'

  if (!intelligence.quote || !points.length) {
    return <section className="stock-chart-card panel stock-chart-unavailable" aria-label="行情图表不可用">
      <ChartNoAxesCombined size={28} />
      <div><strong>行情图表暂不可用</strong><p>该股票尚未获得带来源、时间与完整度证明的行情序列。Copilot 不会生成替代曲线。</p></div>
    </section>
  }

  return <section className="stock-chart-card panel">
    <div className="quote-strip">
      <div>
        <strong>¥{intelligence.quote.price.toFixed(2)}</strong>
        <span className={intelligence.quote.change >= 0 ? 'up' : 'down'}>{signed(intelligence.quote.change)} {signed(intelligence.quote.changePct)}%</span>
        <small>{sourceLabel} · {formatTime(intelligence.updatedAt)}</small>
      </div>
      <div>
        <span>前收 ¥{intelligence.quote.previousClose.toFixed(2)}</span>
        <small>{isDemo ? '只用于交互验收，不是实时行情' : `数据截至 ${formatTime(intelligence.source?.dataThrough ?? null)}`}</small>
      </div>
      <div className={`forecast-quote-state ${forecastVisible ? 'visible' : ''}`}>
        <span>行情线预测</span>
        <strong>{intelligence.forecast ? forecastStatusLabel(intelligence.forecast.readiness.status) : '暂无正式预测'}</strong>
        <small>{intelligence.forecast ? `${intelligence.forecast.horizonLabel} · ${forecastBlocked ? '未达到可视化使用条件' : forecastVisible ? '研究图层已展开' : '手工展开后查看情景'}` : '不以当前行情临时生成曲线'}</small>
      </div>
    </div>
    <div className="chart-toolbar">
      <div className="range-tabs" aria-label="行情周期" role="tablist">
        {rangeOptions.map((option) => <button aria-selected={range === option} className={range === option ? 'active' : ''} key={option} onClick={() => onRangeChange(option)} role="tab" type="button">{option}</button>)}
      </div>
      <div className="chart-tools">
        <button aria-pressed={forecastVisible} disabled={!forecastMatchesRange || forecastBlocked} onClick={onToggleForecast} type="button">
          {forecastVisible ? <Eye size={15} /> : <EyeOff size={15} />}{!forecastMatchesRange ? '该周期无预测' : forecastBlocked ? '预测已阻断' : forecastVisible ? '预测已显示' : '显示预测'}
        </button>
        <button aria-label="图表指标说明" title="价格、成交量与预测区间" type="button"><SlidersHorizontal size={15} /></button>
      </div>
    </div>
    <p className="sr-only" id={summaryId}>展示 {intelligence.name} {range} 价格与成交量{forecastVisible ? `，以及${intelligence.forecast?.horizonLabel ?? '未提供期限'}的研究中位线和包络` : ''}。</p>
    <div aria-describedby={summaryId} aria-label={`${intelligence.name} ${range} ${forecastVisible ? '行情与研究预测图' : '行情图'}`} className="stock-chart-visual" role="img">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartData} margin={{ top: 20, right: 18, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="rgba(222, 230, 233, .055)" vertical={false} />
          <XAxis axisLine={false} dataKey="label" interval="preserveStartEnd" minTickGap={36} tick={{ fill: '#69747c', fontSize: 10 }} tickLine={false} />
          <YAxis axisLine={false} domain={['auto', 'auto']} orientation="right" tick={{ fill: '#69747c', fontSize: 10 }} tickFormatter={(value) => Number(value).toFixed(Number(value) > 100 ? 0 : 2)} tickLine={false} width={48} yAxisId="price" />
          <YAxis domain={[0, (dataMax: number) => dataMax * 7]} hide yAxisId="volume" />
          <Tooltip content={<StockChartTooltip isDemo={isDemo} />} cursor={{ stroke: 'rgba(215, 226, 230, .14)' }} />
          {forecastVisible ? <Area dataKey="forecastWideEnvelope" fill="rgba(205, 169, 95, .09)" isAnimationActive={false} stroke="transparent" type="monotone" yAxisId="price" /> : null}
          {forecastVisible ? <Area dataKey="forecastNarrowEnvelope" fill="rgba(205, 169, 95, .16)" isAnimationActive={false} stroke="rgba(205, 169, 95, .18)" strokeWidth={1} type="monotone" yAxisId="price" /> : null}
          <Bar dataKey="volume" fill="rgba(219, 106, 117, .28)" isAnimationActive={false} maxBarSize={5} yAxisId="volume" />
          {lastHistorical ? <ReferenceLine stroke="rgba(215, 226, 230, .14)" strokeDasharray="2 6" x={lastHistorical.label} yAxisId="price" /> : null}
          <Line dataKey="price" dot={false} isAnimationActive={false} stroke="#df707b" strokeWidth={2} type="monotone" yAxisId="price" />
          {forecastVisible ? <Line dataKey="forecastMedian" dot={false} isAnimationActive={false} stroke="#cdaa62" strokeDasharray="5 6" strokeWidth={1.8} type="monotone" yAxisId="price" /> : null}
          <ReferenceLine stroke="rgba(215, 226, 230, .22)" strokeDasharray="5 7" y={intelligence.quote.previousClose} yAxisId="price" />
        </ComposedChart>
      </ResponsiveContainer>
      {forecastVisible ? <div className="forecast-watermark"><strong>研究情景</strong><span>{intelligence.forecast?.horizonLabel ?? '期限未提供'}</span></div> : null}
      {isDemo ? <div className="demo-chart-watermark">演示数据 · 不可用于交易</div> : null}
    </div>
    <div className="chart-legend-row">
      <span><i className="history" />历史/盘中价格</span>
      {forecastVisible ? <span><i className="median" />预测中位线</span> : null}
      {forecastVisible ? <span><i className="band" />窄幅 / 宽幅研究包络</span> : null}
      <em>{forecastVisible ? '未校准包络不使用概率或置信度标签' : forecastBlocked ? '预测未达到可视化使用条件' : '预测图层默认关闭，需手工展开'}</em>
    </div>
  </section>
}

function StockChartTooltip({ active, payload, label, isDemo }: { active?: boolean; payload?: Array<{ dataKey: keyof StockSeriesPoint; payload: StockSeriesPoint }>; label?: string; isDemo: boolean }) {
  if (!active || !payload?.length) return null
  const point = payload[0]?.payload
  if (!point) return null
  return <div className="stock-chart-tooltip">
    <strong>{label}</strong>
    {point.price !== null ? <span>价格 ¥{point.price.toFixed(2)}</span> : null}
    {point.volume !== null ? <span>成交量 {formatVolume(point.volume)}</span> : null}
    {point.forecastMedian !== null ? <span>情景中位 ¥{point.forecastMedian.toFixed(2)}</span> : null}
    {point.forecastNarrowEnvelope ? <span>窄幅包络 ¥{point.forecastNarrowEnvelope[0].toFixed(2)}–{point.forecastNarrowEnvelope[1].toFixed(2)}</span> : null}
    {point.forecastWideEnvelope ? <span>宽幅包络 ¥{point.forecastWideEnvelope[0].toFixed(2)}–{point.forecastWideEnvelope[1].toFixed(2)}</span> : null}
    <em>{isDemo ? '演示数据 · 不构成建议' : '正式投影 · 仍需人工复核'}</em>
  </div>
}

function signed(value: number) { return `${value >= 0 ? '+' : ''}${value.toFixed(2)}` }
function formatTime(value: string | null) { return value ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '无时间' }
function formatVolume(value: number) { return value >= 10_000 ? `${(value / 10_000).toFixed(1)} 万` : value.toLocaleString('zh-CN') }
function forecastStatusLabel(status: ForecastReadinessStatus) {
  return status === 'decision_support_ready' ? '预测门禁已通过' : status === 'illustrative_only' ? '研究演示 · 概率停显' : '预测已阻断'
}
