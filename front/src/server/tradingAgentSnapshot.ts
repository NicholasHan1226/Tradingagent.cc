import { access, readFile, readdir } from 'node:fs/promises'
import { basename, join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { tradingAgentReadModelSources, type TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel.ts'
import type { ApiStatus } from '../api/types.ts'
import type { HoldingRow, Market, PerformancePoint, SignalRow, SignalStatus } from '../types/dashboard.ts'

type SnapshotOptions = {
  workspaceRoot: string
  signalQueueDir?: string
  now?: Date
}

type PositionRow = {
  ts_code?: string
  quantity?: number
  sellable_quantity?: number
  avg_price?: number
  cost_basis?: number
  side?: string
  price?: number
  pnl?: number
  running_avg_price?: number
  running_cost?: number
  realized_pnl?: number
  entry_date?: string
  thesis?: string
}

type PositionPlanFile = {
  positions?: PositionRow[]
}

type SignalFile = {
  ts_code?: string
  symbol?: string
  market?: string
  direction?: string
  status?: string
  confidence?: string | number
  reason?: string
  expected_alpha_bps?: number
  alpha_bps?: number
  discovered_at?: string
  scored_at?: string
  debated_at?: string
  risk_checked_at?: string
  triggered_at?: string
  created_at?: string
  updated_at?: string
}

type PerformanceReviewRow = {
  trade_date?: string
  date?: string
  day?: string
  simulated_return_pct?: number
  return_pct?: number
  pnl_pct?: number
  mtd_return_pct?: number
  target_return_pct?: number
  target_pct?: number
  benchmark_return_pct?: number
  benchmark_pct?: number
  opportunity_gap_pct?: number
  missed_alpha_pct?: number
}

const SIGNAL_BUCKETS = ['pending', 'claimed', 'running', 'filled', 'expired', 'cancelled', 'failed', 'partial']
const MAX_SIGNALS_PER_BUCKET = 80

export async function readTradingAgentSnapshot({
  workspaceRoot,
  signalQueueDir,
  now = new Date(),
}: SnapshotOptions): Promise<TradingAgentReadModelSnapshot> {
  const projectRoot = resolveTradingAgentRoot(workspaceRoot)
  const queueRoot = signalQueueDir ?? join(projectRoot, 'signals')
  const generatedAt = now.toISOString()
  const positionsPath = join(projectRoot, 'signals/positions')
  const positionPlanPath = toProjectPath(projectRoot, tradingAgentReadModelSources.capitalPlan)
  const filledSignalsPath = join(projectRoot, 'signals/filled')
  const reviewPath = toProjectPath(projectRoot, tradingAgentReadModelSources.review)
  const holdings = await readPositionSnapshots(positionsPath)
  const fallbackHoldings = holdings.length > 0 ? holdings : await readPositionPlan(positionPlanPath)
  const signals = await readSignalQueue(queueRoot)
  const performance = await readPerformanceSeries(reviewPath)
  const hasOrders = await directoryHasJson(filledSignalsPath)
  const hasPlan = await fileExists(positionPlanPath)
  const hasReview = await fileExists(reviewPath)

  return {
    mode: 'simulated',
    generatedAt,
    domains: {
      performance: domainHealth(performance.length > 0 || hasOrders || hasReview || hasPlan ? 'ready' : 'empty', generatedAt),
      signals: domainHealth(signals.length > 0 ? 'ready' : 'empty', generatedAt),
      holdings: domainHealth(fallbackHoldings.length > 0 ? 'ready' : 'empty', generatedAt),
      decisions: domainHealth(hasReview ? 'ready' : 'empty', generatedAt),
      risk: domainHealth(fallbackHoldings.length > 0 || signals.length > 0 ? 'ready' : 'empty', generatedAt),
    },
    performance,
    holdings: fallbackHoldings,
    signals,
    sourceRefs: tradingAgentReadModelSources,
  }
}

function resolveTradingAgentRoot(workspaceRoot: string) {
  return basename(workspaceRoot).toLowerCase() === 'tradingagent' ? workspaceRoot : join(workspaceRoot, 'TradingAgent')
}

function toProjectPath(projectRoot: string, sourceRef: string) {
  return join(projectRoot, sourceRef.replace(/^TradingAgent\//, ''))
}

async function readPerformanceSeries(path: string): Promise<PerformancePoint[]> {
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    return lines
      .map((line) => parsePerformanceRow(JSON.parse(line) as PerformanceReviewRow))
      .filter((row): row is PerformancePoint => Boolean(row))
  } catch {
    return []
  }
}

function parsePerformanceRow(row: PerformanceReviewRow): PerformancePoint | null {
  const day = formatReviewDay(row.trade_date ?? row.date ?? row.day)
  const simulated = firstNumber(row.simulated_return_pct, row.return_pct, row.pnl_pct, row.mtd_return_pct)
  if (!day || simulated === undefined) return null

  return {
    day,
    simulated,
    target: firstNumber(row.target_return_pct, row.target_pct) ?? 0,
    benchmark: firstNumber(row.benchmark_return_pct, row.benchmark_pct) ?? 0,
    opportunity: firstNumber(row.opportunity_gap_pct, row.missed_alpha_pct) ?? 0,
  }
}

async function readPositionSnapshots(root: string): Promise<HoldingRow[]> {
  try {
    const names = await readdir(root)
    const rows = await Promise.all(
      names.filter((name) => name.endsWith('.json')).map(async (name) => parsePositionRow(await readJson(join(root, name)))),
    )
    return rows.filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

async function readPositionPlan(path: string): Promise<HoldingRow[]> {
  try {
    const lines = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean)
    const latest = JSON.parse(lines.at(-1) ?? '{}') as PositionPlanFile
    return (latest.positions ?? []).map(parsePositionRow).filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return readLegacySimulatedPositions(path)
  }
}

function readLegacySimulatedPositions(path: string): HoldingRow[] {
  try {
    const db = new DatabaseSync(path, { readOnly: true })
    const rows = db.prepare('SELECT * FROM position_ledger_simulated').all() as PositionRow[]
    db.close()

    return rows.map(parsePositionRow).filter((row): row is HoldingRow => Boolean(row))
  } catch {
    return []
  }
}

function parsePositionRow(row: unknown): HoldingRow | null {
  const position = row as PositionRow
  const symbol = position.ts_code
  if (!symbol) return null

  return {
    symbol,
    name: symbol,
    market: inferMarket(symbol),
    weight: formatCost(position.cost_basis ?? position.running_cost ?? position.quantity),
    pnl: formatCurrency(position.pnl ?? position.realized_pnl ?? 0),
    risk: '正常',
    role: position.thesis ?? (position.side ? `${position.side} 持仓` : '模拟盘持仓'),
  }
}

async function readSignalQueue(root: string): Promise<SignalRow[]> {
  const files: Array<{ bucket: string; path: string }> = []

  for (const bucket of SIGNAL_BUCKETS) {
    const dir = join(root, bucket)
    try {
      const names = await readdir(dir)
      files.push(
        ...names
          .filter((name) => name.endsWith('.json'))
          .sort()
          .slice(0, MAX_SIGNALS_PER_BUCKET)
          .map((name) => ({ bucket, path: join(dir, name) })),
      )
    } catch {
      // Missing buckets are expected while execution writeback is not wired.
    }
  }

  const rows = await Promise.all(files.map((file) => readSignalFile(file.path, file.bucket)))
  return rows.filter((row): row is SignalRow => Boolean(row))
}

async function readSignalFile(path: string, bucket: string): Promise<SignalRow | null> {
  try {
    const raw = JSON.parse(await readFile(path, 'utf8')) as SignalFile
    const symbol = raw.ts_code ?? raw.symbol
    if (!symbol) return null

    return {
      symbol,
      name: symbol,
      market: normalizeMarket(raw.market, symbol),
      method: raw.direction ? `${raw.direction}` : '待确认',
      status: mapSignalStatus(raw.status ?? bucket),
      impact: formatAlphaBps(raw.alpha_bps ?? raw.expected_alpha_bps),
      confidence: raw.confidence ? String(raw.confidence) : '--',
      age: formatAge(raw.discovered_at ?? raw.created_at, new Date()),
      reason: raw.reason ?? '等待下一次确认',
      next: mapSignalNext(bucket),
      steps: bucket === 'filled' ? 6 : 5,
      stage: inferSignalStage(raw, bucket),
      stageTimes: formatStageTimes(raw),
      stageLatencyMinutes: calculateStageLatency(raw),
    }
  } catch {
    return null
  }
}

async function readJson(path: string) {
  return JSON.parse(await readFile(path, 'utf8')) as unknown
}

function domainHealth(status: ApiStatus, updatedAt: string) {
  return { status, updatedAt }
}

async function fileExists(path: string) {
  try {
    await access(path)
    return true
  } catch {
    return false
  }
}

async function directoryHasJson(path: string) {
  try {
    const names = await readdir(path)
    return names.some((name) => name.endsWith('.json'))
  } catch {
    return false
  }
}

function inferMarket(symbol: string): Market {
  if (symbol.endsWith('.HK')) return 'HK'
  if (symbol.endsWith('.SH') || symbol.endsWith('.SZ')) return 'A-share'
  if (symbol.endsWith('.US')) return 'US'
  if (symbol.includes('-USD') || symbol.includes('PERP')) return 'Crypto'
  if (symbol.startsWith('PM-')) return 'PM'
  return 'All Markets'
}

function normalizeMarket(market: string | undefined, symbol: string): Market {
  const value = market?.toLowerCase()
  if (value === 'hk') return 'HK'
  if (value === 'a-share' || value === 'ashare' || value === 'a_share' || value === 'cn') return 'A-share'
  if (value === 'us') return 'US'
  if (value === 'crypto') return 'Crypto'
  if (value === 'pm' || value === 'prediction' || value === 'prediction-market') return 'PM'
  return inferMarket(symbol)
}

function mapSignalStatus(status: string): SignalStatus {
  if (status === 'filled' || status === 'executed') return 'executed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'expired' || status === 'failed') return 'missed'
  if (status === 'partial') return 'blocked'
  return 'pending'
}

function mapSignalNext(bucket: string) {
  if (bucket === 'pending') return '等待触发条件'
  if (bucket === 'claimed') return '等待执行确认'
  if (bucket === 'running') return '执行中，等待回执'
  if (bucket === 'filled') return '已进入收益记录'
  return '进入复盘'
}

function inferSignalStage(signal: SignalFile, bucket: string): SignalRow['stage'] {
  const status = signal.status ?? bucket
  if (status === 'filled' || status === 'executed' || signal.triggered_at) return '执行确认'
  if (signal.risk_checked_at || status === 'partial') return '风险筛选'
  if (signal.debated_at) return '交易条件'
  if (signal.scored_at) return '形成信号'
  return '发现机会'
}

function formatStageTimes(signal: SignalFile): SignalRow['stageTimes'] {
  return {
    discovered: formatClock(signal.discovered_at ?? signal.created_at),
    scored: formatClock(signal.scored_at),
    debated: formatClock(signal.debated_at),
    riskChecked: formatClock(signal.risk_checked_at),
    triggered: formatClock(signal.triggered_at),
  }
}

function calculateStageLatency(signal: SignalFile) {
  const start = Date.parse(signal.discovered_at ?? signal.created_at ?? '')
  const end = Date.parse(signal.triggered_at ?? signal.risk_checked_at ?? signal.scored_at ?? signal.updated_at ?? '')
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return undefined
  return Math.round((end - start) / 60000)
}

function formatClock(value?: string) {
  if (!value) return undefined
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return undefined
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(date)
}

function formatAge(value: string | undefined, now: Date) {
  if (!value) return '--'
  const created = Date.parse(value)
  if (!Number.isFinite(created)) return '--'
  const minutes = Math.max(0, Math.round((now.getTime() - created) / 60000))
  if (minutes < 60) return `${minutes}分钟`
  return `${Math.round(minutes / 60)}小时`
}

function formatAlphaBps(value: number | undefined) {
  if (value === undefined) return '--'
  const sign = value >= 0 ? '+' : ''
  return `${sign}${Number(value.toFixed(1))} bps`
}

function firstNumber(...values: Array<number | undefined>) {
  return values.find((value): value is number => typeof value === 'number' && Number.isFinite(value))
}

function formatReviewDay(value?: string) {
  if (!value) return undefined
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (match) return `${Number(match[2])}月${Number(match[3])}日`
  return value
}

function formatCurrency(value: number) {
  const sign = value >= 0 ? '+' : '-'
  return `${sign}$${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function formatCost(value?: number) {
  if (value === undefined) return '--'
  return `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}
