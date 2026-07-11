import { PerformanceChart } from '../components/charts/PerformanceChart'
import { RiskTimeline } from '../components/charts/RiskTimeline'
import { ChartSkeleton, TableSkeleton } from '../components/Skeleton'
import { StatusBoundary } from '../components/StatusBoundary'
import { SignalTable } from '../components/tables/SignalTable'
import { PortfolioLedger } from '../components/terminal/PortfolioLedger'
import { AdaptiveTerminalSurface } from '../components/terminal/AdaptiveTerminalSurface'
import { EvidenceEmptyState } from '../components/terminal/EvidenceEmptyState'
import { ProcessBook } from '../components/terminal/ProcessBook'
import { ProcessCycleLedger } from '../components/terminal/ProcessCycleLedger'
import { ProcessEventLedger } from '../components/terminal/ProcessEventLedger'
import { RiskLedger } from '../components/terminal/RiskLedger'
import { TerminalInspectorSection, TerminalPageShell, TerminalPanelHeader, type TerminalMetric } from '../components/terminal/TerminalPageShell'
import { getActionableSignals, getClosedSignals, getSignalFunnel } from '../lib/dashboard'
import { DRAWDOWN_LIMIT_PCT } from '../lib/dashboardConstants'
import { createPortfolioLedgerRows, createProcessBookRows, createRiskLedgerRows, summarizePortfolioCurrency } from '../lib/terminalViewModels'
import { createProcessEventRows } from '../lib/processEventViewModel'
import { createProcessCycles } from '../lib/processCycleViewModel'
import { translateTerminalValue, type RuntimeHeartbeat } from '../lib/runtimeHeartbeat'
import { getHoldingsEmptyEvidence, getPerformanceDensity } from '../lib/terminalDensity'
import type { ChartEvent, FunnelEvent, HoldingRow, Market, MarketSummary, Page, PerformancePoint, PerformanceRange, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'

export function ThemePage({
  activePage,
  activeMarket,
  data,
  latestPoint,
  performanceRange,
  holdings,
  marketSummary,
  portfolio,
  domainStatus,
  onRetry,
  setPerformanceRange,
  signals,
  events,
  funnelEvents,
  heartbeat,
  snapshotGeneratedAt,
}: {
  activePage: Exclude<Page, '总览'>
  activeMarket: Market
  data: PerformancePoint[]
  events: ChartEvent[]
  funnelEvents: FunnelEvent[]
  heartbeat: RuntimeHeartbeat
  snapshotGeneratedAt: string | null
  holdings: HoldingRow[]
  latestPoint: PerformancePoint
  performanceRange: PerformanceRange
  marketSummary?: MarketSummary
  portfolio: PortfolioSummary | null
  domainStatus: (domain: DataDomain) => DomainStatus
  onRetry: () => void
  setActivePage: (page: Page) => void
  setPerformanceRange: (range: PerformanceRange) => void
  signals: SignalRow[]
}) {
  const drawdown = getCurrentDrawdown(data, portfolio)
  const context = createContextMetrics({ activeMarket, drawdown, heartbeat, holdings, latestPoint, marketSummary, portfolio, signals })
  const completed = getClosedSignals(signals)

  if (activePage === '收益') {
    const density = getPerformanceDensity(data, portfolio)
    const chartHeight = density === 'active' ? 520 : 300
    return (
      <TerminalPageShell
        inspector={<ReturnInspector latestPoint={latestPoint} portfolio={portfolio} signals={signals} />}
        heartbeat={heartbeat}
        metrics={context}
        primary={
          <AdaptiveTerminalSurface density={density} label="收益证据面">
            <section className="terminal-chart-surface">
              <TerminalPanelHeader eyebrow="PERFORMANCE" meta={`${data.length} 个样本 · ${density === 'active' ? '活跃' : density === 'quiet' ? '静默' : '等待'}`} title="模拟盘收益 / 目标 / 基准" />
              <StatusBoundary loading={<ChartSkeleton height={chartHeight} />} onRetry={onRetry} status={domainStatus('performance') === 'empty' ? 'ready' : domainStatus('performance')}>
                {density === 'empty'
                  ? <EvidenceEmptyState model={{ title: '收益证据尚未形成', detail: '快照连接正常；首个可验证收益结果写入后会展开完整图表。', rows: [['收益样本', '0 项'], ['当前收益', '—'], ['最近变化', '尚无'], ['数据时间', heartbeat.snapshotLabel]] }} />
                  : <><PerformanceChart currentTone={getPerformanceTone(latestPoint.simulated)} data={data} events={events} height={chartHeight} latestPoint={latestPoint} onRangeChange={setPerformanceRange} range={performanceRange} showRangeControls />{density === 'quiet' && <PerformanceQuietStrip data={data} latestPoint={latestPoint} portfolio={portfolio} />}</>}
              </StatusBoundary>
            </section>
          </AdaptiveTerminalSurface>
        }
        title="收益终端"
      />
    )
  }

  if (activePage === '过程') {
    const model = createProcessBookRows(getActionableSignals(signals).filter((signal) => signal.status === 'pending'), completed)
    const eventRows = createProcessEventRows(funnelEvents)
    const cycles = createProcessCycles(funnelEvents)
    return (
      <TerminalPageShell
        inspector={<ProcessInspector signals={signals} />}
        heartbeat={heartbeat}
        ledger={<ProcessEventLedger rows={eventRows} />}
        metrics={context}
        primary={<StatusBoundary emptyLabel="当前没有过程记录" loading={<TableSkeleton rows={7} />} onRetry={onRetry} status={domainStatus('signals')}>{cycles.length ? <ProcessCycleLedger rows={cycles} /> : <ProcessBook {...model} />}</StatusBoundary>}
        title="过程终端"
      />
    )
  }

  if (activePage === '持仓') {
    const rows = createPortfolioLedgerRows(holdings)
    const emptyModel = getHoldingsEmptyEvidence({ holdings, signals, portfolio, generatedAt: snapshotGeneratedAt })
    return (
      <TerminalPageShell
        inspector={<PortfolioInspector holdings={holdings} rows={rows} />}
        heartbeat={heartbeat}
        metrics={context}
        primary={<AdaptiveTerminalSurface density={rows.length ? 'active' : 'empty'} label="持仓证据面"><StatusBoundary loading={<TableSkeleton rows={7} />} onRetry={onRetry} status={domainStatus('holdings') === 'empty' ? 'ready' : domainStatus('holdings')}>{rows.length ? <PortfolioLedger rows={rows} /> : <EvidenceEmptyState model={emptyModel} />}</StatusBoundary></AdaptiveTerminalSurface>}
        title="持仓终端"
      />
    )
  }

  if (activePage === '风险') {
    const riskRows = createRiskLedgerRows(signals, {
      performance: domainStatus('performance'), signals: domainStatus('signals'), holdings: domainStatus('holdings'),
      decisions: domainStatus('decisions'), risk: domainStatus('risk'),
    })
    return (
      <TerminalPageShell
        inspector={<RiskInspector drawdown={drawdown} hasRiskData={data.length > 0 || Boolean(portfolio)} holdings={holdings} rows={riskRows} signals={signals} />}
        heartbeat={heartbeat}
        ledger={<RiskLedger rows={riskRows} />}
        metrics={context}
        primary={<section className="terminal-chart-surface risk-chart-surface"><TerminalPanelHeader eyebrow="RISK ENVELOPE" meta={`硬限制 ${DRAWDOWN_LIMIT_PCT.toFixed(0)}%`} title="回撤与保护结果" /><div className="risk-threshold-legend"><span className="warning">预警 5%</span><span className="negative">限制 7%</span></div><StatusBoundary loading={<ChartSkeleton height={360} />} onRetry={onRetry} status={domainStatus('risk')}><RiskTimeline data={data} portfolio={portfolio} /></StatusBoundary></section>}
        title="风险终端"
      />
    )
  }

  return (
    <TerminalPageShell
      inspector={<ReviewInspector signals={completed} />}
      heartbeat={heartbeat}
      metrics={context}
      primary={<section className="terminal-table-panel review-ledger"><TerminalPanelHeader eyebrow="AUTOMATIC REVIEW" meta={`${completed.length} 条`} title="结果与自动复盘" /><StatusBoundary emptyLabel="还没有已关闭过程" loading={<TableSkeleton rows={7} />} onRetry={onRetry} status={domainStatus('signals')}><SignalTable showTools signals={completed} /></StatusBoundary></section>}
      title="复盘终端"
    />
  )
}

function createContextMetrics({ activeMarket, drawdown, heartbeat, holdings, latestPoint, marketSummary, portfolio, signals }: { activeMarket: Market; drawdown: number; heartbeat: RuntimeHeartbeat; holdings: HoldingRow[]; latestPoint: PerformancePoint; marketSummary?: MarketSummary; portfolio: PortfolioSummary | null; signals: SignalRow[] }): TerminalMetric[] {
  const pending = signals.filter((row) => row.status === 'pending').length
  const blocked = signals.filter((row) => row.status === 'blocked').length
  const returnPct = portfolio?.returnPct ?? latestPoint.simulated
  return [
    { label: '市场', value: activeMarket === 'All Markets' ? '全市场' : activeMarket, detail: marketSummary?.runtimeState === 'needs_attention' ? '需要关注' : '只读观测' },
    { label: '组合收益', value: returnPct === undefined ? '—' : `${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%`, tone: returnPct === undefined ? 'muted' : returnPct >= 0 ? 'positive' : 'negative' },
    { label: '最大回撤', value: `-${drawdown.toFixed(2)}%`, tone: drawdown >= 5 ? 'negative' : 'muted' },
    { label: '自动过程', value: pending ? `${pending} 运行中` : heartbeat.state === 'idle' ? '当前空闲' : heartbeat.state === 'stale' ? '快照滞后' : '需要关注', detail: `${blocked} 安全拦截`, tone: pending ? 'warning' : heartbeat.state === 'degraded' ? 'negative' : heartbeat.state === 'stale' ? 'warning' : 'muted' },
    { label: '持仓', value: String(holdings.length), detail: summarizePortfolioCurrency(holdings).label },
  ]
}

function ReturnInspector({ latestPoint, portfolio, signals }: { latestPoint: PerformancePoint; portfolio: PortfolioSummary | null; signals: SignalRow[] }) {
  const ranked = [...signals].map((signal) => ({ label: translateTerminalValue(signal.strategyName ?? signal.method), value: parseImpact(signal.impact) })).filter((row) => row.value !== null && row.value !== 0).sort((a, b) => Math.abs(b.value ?? 0) - Math.abs(a.value ?? 0)).slice(0, 5)
  return <><TerminalInspectorSection title="收益状态"><InspectorRows rows={[['累计收益', signedPercent(portfolio?.returnPct ?? latestPoint.simulated)], ['目标', `${(portfolio?.targetPct ?? latestPoint.target).toFixed(2)}%`], ['已实现', formatMoney(portfolio?.realizedPnl, portfolio?.pnlCurrency)], ['未实现', formatMoney(portfolio?.unrealizedPnl, portfolio?.pnlCurrency)]]} /></TerminalInspectorSection><TerminalInspectorSection title="贡献排名">{ranked.length ? <div className="terminal-ranking">{ranked.map((row) => <div key={row.label}><span>{row.label}</span><i style={{ width: `${Math.min(100, Math.abs(row.value ?? 0) * 8)}%` }} /><strong className={(row.value ?? 0) < 0 ? 'negative' : 'positive'}>{row.value! > 0 ? '+' : ''}{row.value}</strong></div>)}</div> : <p className="terminal-inspector-note">暂无可用收益归因。</p>}</TerminalInspectorSection></>
}

function ProcessInspector({ signals }: { signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const average = averageLatency(signals)
  return <><TerminalInspectorSection title="过程分布"><div className="stage-distribution">{funnel.stages.map((stage) => <div key={stage.label}><span>{stage.label}</span><i style={{ width: `${Math.max(3, (stage.rows.length / Math.max(1, signals.length)) * 100)}%` }} /><strong>{stage.rows.length}</strong></div>)}</div></TerminalInspectorSection><TerminalInspectorSection title="运行质量"><InspectorRows rows={[['发现总数', String(signals.length)], ['结果写回', String(funnel.executed.length)], ['安全拦截', String(funnel.blocked.length)], ['平均耗时', average ? `${average}分钟` : '—']]} /></TerminalInspectorSection></>
}

function PortfolioInspector({ holdings, rows }: { holdings: HoldingRow[]; rows: ReturnType<typeof createPortfolioLedgerRows> }) {
  const total = summarizePortfolioCurrency(holdings)
  return <><TerminalInspectorSection title="组合敞口">{rows.length ? <div className="exposure-ranking">{rows.map((row) => <div key={row.symbol}><span>{row.symbol}</span><i><b style={{ width: row.weight }} /></i><strong>{row.weight}</strong></div>)}</div> : <p className="terminal-inspector-note">当前没有持仓敞口。</p>}</TerminalInspectorSection><TerminalInspectorSection title="组合状态"><InspectorRows rows={[['总市值', total.label], ['资产数', String(rows.length)], ['风险观察', String(rows.filter((row) => row.risk !== '正常').length)], ['计价', total.currency === 'mixed' ? '多币种' : total.currency === 'empty' ? '无持仓' : total.currency]]} /></TerminalInspectorSection></>
}

function RiskInspector({ drawdown, hasRiskData, holdings, rows, signals }: { drawdown: number; hasRiskData: boolean; holdings: HoldingRow[]; rows: ReturnType<typeof createRiskLedgerRows>; signals: SignalRow[] }) {
  const distance = Math.max(0, DRAWDOWN_LIMIT_PCT - drawdown)
  const exposure = summarizeMarketExposure(holdings)
  return <><TerminalInspectorSection title="边界距离"><div className="risk-boundary"><strong className={drawdown >= 5 ? 'negative' : 'positive'}>{hasRiskData ? `-${drawdown.toFixed(2)}%` : '—'}</strong><span>当前最大回撤</span><i><b style={{ width: `${Math.min(100, (drawdown / DRAWDOWN_LIMIT_PCT) * 100)}%` }} /></i><small>距 {DRAWDOWN_LIMIT_PCT.toFixed(0)}% 限制 {hasRiskData ? `${distance.toFixed(2)}%` : '—'}</small></div></TerminalInspectorSection><TerminalInspectorSection title="市场敞口"><InspectorRows rows={exposure.length ? exposure : [['当前敞口', '—']]} /></TerminalInspectorSection><TerminalInspectorSection title="保护结果"><InspectorRows rows={[['安全拦截', String(signals.filter((row) => row.status === 'blocked').length)], ['自动复盘', String(signals.filter((row) => row.status === 'missed').length)], ['事件账本', String(rows.length)], ['风险状态', drawdown >= 5 ? '接近边界' : '正常']]} /></TerminalInspectorSection></>
}

function ReviewInspector({ signals }: { signals: SignalRow[] }) {
  return <><TerminalInspectorSection title="关闭结果"><InspectorRows rows={[['完成', String(signals.filter((row) => row.status === 'executed').length)], ['错过', String(signals.filter((row) => row.status === 'missed').length)], ['终止', String(signals.filter((row) => row.status === 'cancelled').length)], ['部分成交', String(signals.filter((row) => row.queueBucket?.toLowerCase() === 'partial').length)]]} /></TerminalInspectorSection><TerminalInspectorSection title="自动校准"><p className="terminal-inspector-note">系统按已关闭结果保留归因与下一轮规则；本页面只读，不提供人工下单或策略修改入口。</p></TerminalInspectorSection></>
}

function PerformanceQuietStrip({ data, latestPoint, portfolio }: { data: PerformancePoint[]; latestPoint: PerformancePoint; portfolio: PortfolioSummary | null }) {
  const movement = data.length > 1 ? latestPoint.simulated - data[0].simulated : 0
  return <dl aria-label="静默收益证据" className="performance-evidence-strip"><div><dt>有效样本</dt><dd>{data.length}</dd></div><div><dt>最近变化</dt><dd>{Math.abs(movement) < 0.005 ? '无明显变化' : signedPercent(movement)}</dd></div><div><dt>已实现</dt><dd>{formatMoney(portfolio?.realizedPnl, portfolio?.pnlCurrency)}</dd></div><div><dt>未实现</dt><dd>{formatMoney(portfolio?.unrealizedPnl, portfolio?.pnlCurrency)}</dd></div></dl>
}

function summarizeMarketExposure(holdings: HoldingRow[]): [string, string][] {
  const grouped = new Map<Market, { count: number; value: number }>()
  for (const holding of holdings) {
    const current = grouped.get(holding.market) ?? { count: 0, value: 0 }
    grouped.set(holding.market, { count: current.count + 1, value: current.value + (holding.marketValue ?? 0) })
  }
  return [...grouped.entries()].map(([market, item]) => [market, item.value > 0 ? `${item.count}项 · ¥${Math.round(item.value).toLocaleString('en-US')}` : `${item.count}项`])
}

function InspectorRows({ rows }: { rows: [string, string][] }) { return <dl className="terminal-inspector-rows">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl> }
function getPerformanceTone(value: number) { return value < -0.005 ? 'negative' as const : value > 0.005 ? 'positive' as const : 'flat' as const }
function signedPercent(value: number) { return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` }
function formatMoney(value?: number, currency: 'USD' | 'CNY' = 'CNY') { return value === undefined ? '—' : `${value >= 0 ? '+' : '-'}${currency === 'USD' ? '$' : '¥'}${Math.abs(value).toLocaleString('en-US')}` }
function parseImpact(value: string) { const parsed = Number(value.replace(/[+,%K¥$]/g, '').trim()); return Number.isFinite(parsed) ? parsed : null }
function averageLatency(signals: SignalRow[]) { const rows = signals.map((row) => row.stageLatencyMinutes).filter((value): value is number => typeof value === 'number' && Number.isFinite(value) && value > 0); return rows.length ? Math.round(rows.reduce((sum, value) => sum + value, 0) / rows.length) : 0 }
function getCurrentDrawdown(data: PerformancePoint[], portfolio: PortfolioSummary | null) { if (portfolio) return Math.abs(portfolio.maxDrawdownPct); if (!data.length) return 0; let peak = data[0]?.simulated ?? 0; return data.reduce((maximum, point) => { peak = Math.max(peak, point.simulated); return Math.max(maximum, peak - point.simulated) }, 0) }
