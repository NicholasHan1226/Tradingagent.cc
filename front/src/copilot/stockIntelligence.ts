import type { CopilotAnalysisMode } from './types.ts'
import { assessForecastReadiness, type ForecastEvidence, type ForecastHorizon, type ForecastModelId, type ForecastReadiness } from './forecastReadiness.ts'
import { buildLinearBaseline } from './forecastBaseline.ts'

export type StockRange = '1D' | '5D' | '1M' | '6M' | 'YTD' | '1Y'
export type StockDetailTab = 'overview' | 'financials' | 'earnings' | 'holders' | 'forecast' | 'history' | 'analysis'

export type StockSeriesPoint = {
  key: string
  label: string
  price: number | null
  volume: number | null
  forecastMedian: number | null
  forecastNarrowEnvelope: [number, number] | null
  forecastWideEnvelope: [number, number] | null
}

export type StockEvent = {
  id: string
  kind: 'announcement' | 'news' | 'sentiment'
  title: string
  summary: string
  source: string
  publishedAt: string
  sentiment: 'positive' | 'neutral' | 'negative'
  relatedSymbols: string[]
  url: string | null
}

export type StockSentimentSummary = {
  total: number
  positive: number
  neutral: number
  negative: number
  tone: '偏积极' | '分歧' | '偏谨慎' | '暂无舆论'
  latestPublishedAt: string | null
}

export type StockIntelligence = {
  symbol: string
  name: string
  mode: CopilotAnalysisMode
  updatedAt: string | null
  quote: {
    price: number
    previousClose: number
    change: number
    changePct: number
    open: number
    high: number
    low: number
    volume: number
    turnoverRate: number
    peTtm: number | null
    marketCapCny: number
  } | null
  company: {
    exchange: 'SH' | 'SZ'
    industry: string
    area: string
    listingDate: string
    description: string
  } | null
  series: Record<StockRange, StockSeriesPoint[]>
  forecast: {
    mode: 'shadow_uncalibrated' | 'calibrated_research'
    horizon: ForecastHorizon
    horizonLabel: string
    directionalView: '偏强' | '均衡' | '偏弱'
    modelId: ForecastModelId
    evidence: ForecastEvidence
    readiness: ForecastReadiness
    takeaway: string
    drivers: string[]
    caveat: string
  } | null
  events: StockEvent[]
}

type DemoStockConfig = {
  symbol: string
  name: string
  price: number
  previousClose: number
  seed: number
  volatility: number
  drift: number
  industry: string
  area: string
  listingDate: string
  description: string
  forecast: StockIntelligence['forecast']
  events: StockEvent[]
}

const DEMO_TIME = '2026-08-01T07:00:00.000Z'

const demoConfigs: Record<string, DemoStockConfig> = {
  '000400.SZ': {
    symbol: '000400.SZ', name: '许继电气', price: 31.42, previousClose: 30.76, seed: 2.4, volatility: 0.018, drift: 0.0018,
    industry: '电网设备', area: '河南', listingDate: '1997-04-18',
    description: '演示资料：聚焦电力装备与电网自动化业务。公司资料仅用于界面验收，不代表正式基本面结论。',
    forecast: {
      mode: 'shadow_uncalibrated', horizon: 'm30', horizonLabel: '未来 30 分钟', directionalView: '偏强', modelId: 'linear_ridge_baseline',
      ...illustrativeForecastGate('m30', 'linear_ridge_baseline'),
      takeaway: '演示情景偏强，但价格接近观察压力区；更适合等待量价确认，而不是追价。',
      drivers: ['价格结构与短期动量', '成交量相对变化', '演示行业强弱线索', '公告事件的方向标签'],
      caveat: '这是未做样本外校准的研究情景权重，不是上涨概率、目标价或收益承诺。',
    },
    events: [
      demoEvent('xj-ann-1', 'announcement', '演示公告：项目进展提示', '用于展示公告如何按股票代码进入事件时间线；正式内容须由 TradingDatas receipt 与来源链接验证。', '演示公告源', '2026-07-31T09:18:00+08:00', 'positive', '000400.SZ'),
      demoEvent('xj-news-1', 'news', '演示新闻：电网设备板块活跃', '板块线索只能解释价格波动，不能单独形成买入理由。', '演示新闻源', '2026-07-31T13:36:00+08:00', 'positive', '000400.SZ'),
      demoEvent('xj-sent-1', 'sentiment', '讨论热度上升，分歧同步扩大', '热度提升但观点并不一致，系统将分歧保留为反对证据。', '演示舆情聚合', '2026-07-31T14:10:00+08:00', 'neutral', '000400.SZ'),
    ],
  },
  '002594.SZ': {
    symbol: '002594.SZ', name: '比亚迪', price: 112.86, previousClose: 114.22, seed: 4.1, volatility: 0.022, drift: -0.0004,
    industry: '乘用车', area: '广东', listingDate: '2011-06-30',
    description: '演示资料：新能源汽车与相关产业链公司。页面不把产业地位直接转换为交易结论。',
    forecast: {
      mode: 'shadow_uncalibrated', horizon: 'm30', horizonLabel: '未来 30 分钟', directionalView: '均衡', modelId: 'linear_ridge_baseline',
      ...illustrativeForecastGate('m30', 'linear_ridge_baseline'),
      takeaway: '演示情景偏震荡，方向优势不足；等待结构重新确认比提前押注更合适。',
      drivers: ['波动率状态', '价格与均线距离', '板块相对强弱', '事件情绪分歧'],
      caveat: '情景权重未校准，不能解释为真实胜率。',
    },
    events: [
      demoEvent('byd-ann-1', 'announcement', '演示公告：月度经营数据说明', '公告摘要仅展示关联形态，正式数值不会从演示 fixture 进入决策。', '演示公告源', '2026-07-30T19:42:00+08:00', 'neutral', '002594.SZ'),
      demoEvent('byd-news-1', 'news', '演示新闻：汽车板块出现轮动', '板块轮动与个股表现不同步，需结合价格结构验证。', '演示新闻源', '2026-07-31T10:24:00+08:00', 'negative', '002594.SZ'),
    ],
  },
  '601899.SH': {
    symbol: '601899.SH', name: '紫金矿业', price: 19.38, previousClose: 19.02, seed: 6.3, volatility: 0.017, drift: 0.0012,
    industry: '工业金属', area: '福建', listingDate: '2008-04-25',
    description: '演示资料：全球矿产资源开发企业，价格与商品周期具有较强关联。',
    forecast: {
      mode: 'shadow_uncalibrated', horizon: 'm30', horizonLabel: '未来 30 分钟', directionalView: '偏强', modelId: 'linear_ridge_baseline',
      ...illustrativeForecastGate('m30', 'linear_ridge_baseline'),
      takeaway: '演示情景温和偏强，仍需商品价格和股价方向共同确认。',
      drivers: ['商品价格代理', '周期股相对强度', '量价结构', '宏观风险标签'],
      caveat: '情景权重没有经过真实资金样本外校准，仅用于研究界面。',
    },
    events: [
      demoEvent('zj-news-1', 'news', '演示新闻：有色板块随商品价格波动', '宏观叙事必须与可验证商品数据共同使用。', '演示新闻源', '2026-07-31T11:08:00+08:00', 'positive', '601899.SH'),
      demoEvent('zj-sent-1', 'sentiment', '讨论集中在周期持续性', '正面观点较多，但主要风险仍是商品趋势反转。', '演示舆情聚合', '2026-07-31T14:32:00+08:00', 'neutral', '601899.SH'),
    ],
  },
  '600519.SH': {
    symbol: '600519.SH', name: '贵州茅台', price: 1438.2, previousClose: 1456.8, seed: 8.6, volatility: 0.013, drift: -0.0008,
    industry: '白酒', area: '贵州', listingDate: '2001-08-27',
    description: '演示资料：白酒行业公司。长期质量线索与当前交易条件在 Copilot 中分开呈现。',
    forecast: {
      mode: 'shadow_uncalibrated', horizon: 'm30', horizonLabel: '未来 30 分钟', directionalView: '均衡', modelId: 'linear_ridge_baseline',
      ...illustrativeForecastGate('m30', 'linear_ridge_baseline'),
      takeaway: '演示情景缺少方向优势；以等待估值与趋势共同改善为主。',
      drivers: ['趋势斜率', '波动收敛程度', '消费板块相对强弱', '事件情绪'],
      caveat: '中性权重较高只表示演示模型分歧，不能理解为价格不会波动。',
    },
    events: [
      demoEvent('mt-news-1', 'news', '演示新闻：消费板块表现分化', '板块分化下，个股需要独立条件而不是使用行业平均判断。', '演示新闻源', '2026-07-31T10:48:00+08:00', 'neutral', '600519.SH'),
    ],
  },
}

export function getDemoStockIntelligence(symbol: string): StockIntelligence | null {
  const config = demoConfigs[symbol]
  if (!config) return null
  const change = config.price - config.previousClose
  const series = Object.fromEntries((['1D', '5D', '1M', '6M', 'YTD', '1Y'] as StockRange[])
    .map((range) => [range, buildSeries(config, range)])) as Record<StockRange, StockSeriesPoint[]>
  const terminalForecast = series['1D'].findLast((point) => point.forecastMedian !== null)?.forecastMedian ?? config.price
  const directionalView = terminalForecast > config.price * 1.003 ? '偏强' : terminalForecast < config.price * 0.997 ? '偏弱' : '均衡'
  return {
    symbol: config.symbol,
    name: config.name,
    mode: 'demo_fixture',
    updatedAt: DEMO_TIME,
    quote: {
      price: config.price,
      previousClose: config.previousClose,
      change,
      changePct: (change / config.previousClose) * 100,
      open: round(config.previousClose * (1 + Math.sin(config.seed) * 0.004)),
      high: round(Math.max(config.price, config.previousClose) * 1.012),
      low: round(Math.min(config.price, config.previousClose) * 0.988),
      volume: Math.round(18_000_000 + config.seed * 2_100_000),
      turnoverRate: round(1.24 + config.seed * 0.11),
      peTtm: round(16 + config.seed * 2.1),
      marketCapCny: Math.round(config.price * (3_200_000_000 + config.seed * 180_000_000)),
    },
    company: {
      exchange: config.symbol.endsWith('.SH') ? 'SH' : 'SZ', industry: config.industry, area: config.area,
      listingDate: config.listingDate, description: config.description,
    },
    series,
    forecast: config.forecast ? { ...config.forecast, directionalView, takeaway: demoTakeaway(directionalView) } : null,
    events: config.events,
  }
}

export function unavailableStockIntelligence(symbol: string, name: string): StockIntelligence {
  return {
    symbol, name, mode: 'analysis_unavailable', updatedAt: null, quote: null, company: null,
    series: { '1D': [], '5D': [], '1M': [], '6M': [], YTD: [], '1Y': [] },
    forecast: null, events: [],
  }
}

export function summarizeStockSentiment(events: StockEvent[]): StockSentimentSummary {
  const summary = events.reduce((current, event) => ({
    ...current,
    [event.sentiment]: current[event.sentiment] + 1,
  }), { positive: 0, neutral: 0, negative: 0 })
  const total = events.length
  const latestPublishedAt = events
    .map((event) => event.publishedAt)
    .filter((value) => !Number.isNaN(Date.parse(value)))
    .sort((left, right) => Date.parse(right) - Date.parse(left))[0] ?? null
  const tone = total === 0
    ? '暂无舆论'
    : summary.positive > summary.negative && summary.positive >= Math.ceil(total / 2)
      ? '偏积极'
      : summary.negative > summary.positive && summary.negative >= Math.ceil(total / 2)
        ? '偏谨慎'
        : '分歧'
  return { total, ...summary, tone, latestPublishedAt }
}

function buildSeries(config: DemoStockConfig, range: StockRange): StockSeriesPoint[] {
  const definition = rangeDefinition(range)
  const count = definition.count
  const historical = Array.from({ length: count }, (_, index) => {
    const distance = index - (count - 1)
    const wave = Math.sin((index + config.seed) * 0.57) * config.volatility
      + Math.cos((index + config.seed) * 0.19) * config.volatility * 0.55
    const scale = range === '1D' ? 0.34 : range === '5D' ? 0.55 : 1
    const price = config.price * (1 + distance * config.drift * scale + wave * scale)
    return round(price)
  })
  const offset = config.price - (historical.at(-1) ?? config.price)
  const normalized = historical.map((price) => round(price + offset))
  const points: StockSeriesPoint[] = normalized.map((price, index) => ({
    key: `h-${range}-${index}`,
    label: definition.label(index, count),
    price,
    volume: Math.max(1, Math.round((7 + Math.abs(Math.sin(index + config.seed)) * 21 + (index % 9 === 0 ? 8 : 0)) * 100_000)),
    forecastMedian: null,
    forecastNarrowEnvelope: null,
    forecastWideEnvelope: null,
  }))

  const horizon = range === '1D' ? 8 : range === '5D' ? 5 : 6
  const baseline = buildLinearBaseline(normalized, horizon)
  const firstForecast: StockSeriesPoint = {
    ...points.at(-1)!, key: `f-${range}-0`, price: config.price, volume: null, forecastMedian: config.price,
    forecastNarrowEnvelope: [config.price, config.price], forecastWideEnvelope: [config.price, config.price],
  }
  const future = Array.from({ length: horizon }, (_, index) => {
    const step = index + 1
    const estimate = baseline?.points[index]
    const median = estimate?.median ?? config.price
    return {
      key: `f-${range}-${step}`,
      label: definition.forecastLabel(step),
      price: null,
      volume: null,
      forecastMedian: round(median),
      forecastNarrowEnvelope: estimate?.narrowEnvelope ?? [config.price, config.price],
      forecastWideEnvelope: estimate?.wideEnvelope ?? [config.price, config.price],
    }
  })
  return [...points, firstForecast, ...future]
}

function rangeDefinition(range: StockRange) {
  const labelByRange: Record<StockRange, { count: number; label: (index: number, count: number) => string; forecastLabel: (step: number) => string }> = {
    '1D': {
      count: 49,
      label: (index) => {
        const minute = index < 24 ? 570 + index * 5 : 780 + (index - 24) * 5
        return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`
      },
      forecastLabel: (step) => `+${step * 5}m`,
    },
    '5D': { count: 40, label: (index) => `D${Math.floor(index / 8) + 1} ${index % 8 === 0 ? '开' : ''}`.trim(), forecastLabel: (step) => `未来${step}日` },
    '1M': { count: 22, label: (index) => `07/${String(index + 1).padStart(2, '0')}`, forecastLabel: (step) => `未来${step}日` },
    '6M': { count: 52, label: (index) => `${String(Math.floor(index / 9) + 2).padStart(2, '0')}月`, forecastLabel: (step) => `未来${step}周` },
    YTD: { count: 62, label: (index) => `${String(Math.floor(index / 10) + 1).padStart(2, '0')}月`, forecastLabel: (step) => `未来${step}周` },
    '1Y': { count: 72, label: (index) => `${String((index % 12) + 1).padStart(2, '0')}月`, forecastLabel: (step) => `未来${step}周` },
  }
  return labelByRange[range]
}

function demoEvent(id: string, kind: StockEvent['kind'], title: string, summary: string, source: string, publishedAt: string, sentiment: StockEvent['sentiment'], symbol: string): StockEvent {
  return { id, kind, title, summary, source, publishedAt, sentiment, relatedSymbols: [symbol], url: null }
}

function round(value: number) { return Math.round(value * 100) / 100 }

function demoTakeaway(view: '偏强' | '均衡' | '偏弱') {
  if (view === '偏强') return '线性基线的短期斜率偏强；仍需量价和正式事件证据确认，不能追价。'
  if (view === '偏弱') return '线性基线的短期斜率偏弱；优先观察失效条件，不提前猜测反转。'
  return '线性基线没有形成清晰方向优势；等待结构重新确认比提前押注更合适。'
}

function illustrativeForecastGate(horizon: ForecastHorizon, modelId: ForecastModelId) {
  const evidence: ForecastEvidence = {
    sourceMode: 'demo_fixture', horizon, modelId,
    modelManifestBound: true, pointInTimeVerified: false, frozenOosReceiptBound: false,
    calibrationProofAccepted: false, effectiveIndependentSamples: 0,
    intervalCoverageVerified: false, costPolicyBound: false,
  }
  return { evidence, readiness: assessForecastReadiness(evidence) }
}
