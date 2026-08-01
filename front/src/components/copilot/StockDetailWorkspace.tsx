import { useMemo, useState } from 'react'
import {
  AlertTriangle, Building2, CalendarDays, Check, CircleDollarSign, FileText, Gauge, MessageSquareText,
  Newspaper, ShieldAlert, Sparkles, TrendingDown, TrendingUp,
} from 'lucide-react'
import type { CopilotAnalysis, CopilotDecisionAction, CopilotHolding } from '../../copilot/types'
import type { StockDetailTab, StockEvent, StockIntelligence, StockRange } from '../../copilot/stockIntelligence'
import { StockMarketChart } from './StockMarketChart'

const tabs: Array<{ key: StockDetailTab; label: string }> = [
  { key: 'overview', label: '概述' },
  { key: 'financials', label: '财务数据' },
  { key: 'earnings', label: '收益' },
  { key: 'holders', label: '持有者' },
  { key: 'forecast', label: '预测' },
  { key: 'history', label: '历史数据' },
  { key: 'analysis', label: '分析' },
]

export function StockDetailWorkspace({ analysis, intelligence, holding, latestDecision, onRecordDecision }: {
  analysis: CopilotAnalysis
  intelligence: StockIntelligence
  holding?: CopilotHolding
  latestDecision: string
  onRecordDecision: (action: CopilotDecisionAction) => void
}) {
  const [activeTab, setActiveTab] = useState<StockDetailTab>('overview')
  const [range, setRange] = useState<StockRange>('1D')
  const [showForecast, setShowForecast] = useState(true)
  const stockKey = `${analysis.symbol}-${intelligence.mode}`

  return <section className="stock-workspace" data-stock={stockKey}>
    <header className="stock-detail-header">
      <div className="stock-identity">
        <span className="stock-logo" aria-hidden="true">{analysis.name.slice(0, 1)}</span>
        <div><h2>{analysis.name}</h2><p>{analysis.symbol} · {intelligence.company?.exchange === 'SH' ? '上海证券交易所' : intelligence.company?.exchange === 'SZ' ? '深圳证券交易所' : 'A 股'}</p></div>
        <span className={`source-badge ${analysis.mode}`}>{analysisModeLabel(analysis.mode)}</span>
      </div>
      <div className="stock-quick-state">
        <span><small>系统判断</small><strong>{analysis.verdict}</strong></span>
        <span><small>你的持仓</small><strong>{holding ? `${holding.quantity.toLocaleString('zh-CN')} 股` : '未持有'}</strong></span>
        <span><small>最近决定</small><strong>{latestDecision}</strong></span>
      </div>
    </header>

    <nav className="stock-detail-tabs" aria-label="个股详情" role="tablist">
      {tabs.map((tab) => <button aria-selected={activeTab === tab.key} className={activeTab === tab.key ? 'active' : ''} key={tab.key} onClick={() => setActiveTab(tab.key)} role="tab" type="button">{tab.label}</button>)}
    </nav>

    <div className="stock-tab-content" key={`${stockKey}-${activeTab}`}>
      {activeTab === 'overview' ? <Overview
        analysis={analysis}
        intelligence={intelligence}
        range={range}
        showForecast={showForecast}
        onRangeChange={setRange}
        onToggleForecast={() => setShowForecast((value) => !value)}
      /> : null}
      {activeTab === 'forecast' ? <ForecastPanel intelligence={intelligence} range={range} showForecast={showForecast} onRangeChange={setRange} onToggleForecast={() => setShowForecast((value) => !value)} /> : null}
      {activeTab === 'history' ? <HistoryPanel intelligence={intelligence} range={range} onRangeChange={setRange} /> : null}
      {activeTab === 'analysis' ? <AnalysisPanel analysis={analysis} /> : null}
      {activeTab === 'financials' ? <FinancialPanel intelligence={intelligence} /> : null}
      {activeTab === 'earnings' ? <UnavailablePanel icon={<CalendarDays size={22} />} title="业绩事件数据尚未接入" detail="当前 TradingAgent 快照没有经过验证的业绩预告、业绩快报和财报时间线合同。该页保留产品入口，但不生成演示结论。" /> : null}
      {activeTab === 'holders' ? <UnavailablePanel icon={<Building2 size={22} />} title="持有者数据尚未接入" detail="股东与机构持仓需要独立的数据来源、披露日期和修订口径；当前页面不会从新闻或股价反推持仓变化。" /> : null}
    </div>

    <div className="decision-bar">
      <div><strong>你的决定</strong><span>只写入人工决策账本，不会触发任何订单</span></div>
      <button className="ghost-button" onClick={() => onRecordDecision('skipped')} type="button">暂不交易</button>
      <button className="secondary-button" onClick={() => onRecordDecision('observing')} type="button">继续观察</button>
      <button className="primary-button" disabled={analysis.mode === 'analysis_unavailable'} onClick={() => onRecordDecision('planned')} type="button">加入人工计划</button>
    </div>
  </section>
}

function Overview({ analysis, intelligence, range, showForecast, onRangeChange, onToggleForecast }: {
  analysis: CopilotAnalysis
  intelligence: StockIntelligence
  range: StockRange
  showForecast: boolean
  onRangeChange: (range: StockRange) => void
  onToggleForecast: () => void
}) {
  return <>
    <StockMarketChart intelligence={intelligence} range={range} showForecast={showForecast} onRangeChange={onRangeChange} onToggleForecast={onToggleForecast} />
    <MetricGrid intelligence={intelligence} />
    <section className="significant-moves panel">
      <SectionHeading eyebrow="PRICE & EVENT CONTEXT" title="显著变化与关联事件" />
      <EventTimeline events={intelligence.events} unavailable={intelligence.mode === 'analysis_unavailable'} />
    </section>
    <section className="story-analysis panel">
      <SectionHeading eyebrow="STORIES & ANALYSIS" title="公告、新闻与舆情" />
      <EventCards events={intelligence.events} unavailable={intelligence.mode === 'analysis_unavailable'} />
    </section>
    <section className="key-questions panel">
      <SectionHeading eyebrow="KEY QUESTIONS" title="关键问题：多空证据并列" />
      <QuestionRows analysis={analysis} />
    </section>
  </>
}

function ForecastPanel({ intelligence, range, showForecast, onRangeChange, onToggleForecast }: {
  intelligence: StockIntelligence
  range: StockRange
  showForecast: boolean
  onRangeChange: (range: StockRange) => void
  onToggleForecast: () => void
}) {
  if (!intelligence.forecast) return <UnavailablePanel icon={<Sparkles size={22} />} title="预测暂不可用" detail="该股票没有经过来源验证的历史序列，也没有可回放的预测输入。Copilot 不会只凭名称生成概率。" />
  const { forecast } = intelligence
  return <>
    <section className="forecast-disclaimer panel">
      <div><ShieldAlert size={18} /><span><strong>研究情景 · 未校准</strong><small>{forecast.horizonLabel}</small></span></div>
      <p>{forecast.caveat}</p>
    </section>
    <StockMarketChart intelligence={intelligence} range={range} showForecast={showForecast} onRangeChange={onRangeChange} onToggleForecast={onToggleForecast} />
    <section className="forecast-grid">
      <div className="panel scenario-card">
        <SectionHeading eyebrow="SCENARIO WEIGHTS" title="方向情景权重" />
        <div className="scenario-track" aria-label={`向上 ${forecast.scenarioWeights.up}，震荡 ${forecast.scenarioWeights.neutral}，向下 ${forecast.scenarioWeights.down}`}>
          <i className="up" style={{ width: `${forecast.scenarioWeights.up}%` }} />
          <i className="neutral" style={{ width: `${forecast.scenarioWeights.neutral}%` }} />
          <i className="down" style={{ width: `${forecast.scenarioWeights.down}%` }} />
        </div>
        <div className="scenario-values"><span><TrendingUp size={15} />向上 <strong>{forecast.scenarioWeights.up}%</strong></span><span>震荡 <strong>{forecast.scenarioWeights.neutral}%</strong></span><span><TrendingDown size={15} />向下 <strong>{forecast.scenarioWeights.down}%</strong></span></div>
        <p>{forecast.takeaway}</p>
      </div>
      <div className="panel driver-card">
        <SectionHeading eyebrow="INPUT EXPLAINER" title="本轮使用的分析维度" />
        <ul>{forecast.drivers.map((driver) => <li key={driver}><Check size={14} />{driver}</li>)}</ul>
        <small>正式上线前必须补齐样本外校准、覆盖率、漂移和费用后表现。</small>
      </div>
    </section>
  </>
}

function HistoryPanel({ intelligence, range, onRangeChange }: { intelligence: StockIntelligence; range: StockRange; onRangeChange: (range: StockRange) => void }) {
  const rows = useMemo(() => intelligence.series[range].filter((point) => point.price !== null).slice(-12).reverse(), [intelligence.series, range])
  if (!rows.length) return <UnavailablePanel icon={<FileText size={22} />} title="历史数据暂不可用" detail="没有可验证的个股价格序列。" />
  return <section className="history-table-panel panel">
    <div className="history-table-heading"><SectionHeading eyebrow="PRICE HISTORY" title="历史数据" /><select aria-label="历史数据周期" onChange={(event) => onRangeChange(event.target.value as StockRange)} value={range}>{(['1D', '5D', '1M', '6M', 'YTD', '1Y'] as StockRange[]).map((item) => <option key={item}>{item}</option>)}</select></div>
    <div className="history-table-wrap"><table><thead><tr><th>时间</th><th>价格</th><th>成交量</th><th>数据类型</th></tr></thead><tbody>{rows.map((row) => <tr key={row.key}><td>{row.label}</td><td>¥{row.price?.toFixed(2)}</td><td>{row.volume?.toLocaleString('zh-CN') ?? '—'}</td><td><span className="demo-data-label">演示数据</span></td></tr>)}</tbody></table></div>
  </section>
}

function AnalysisPanel({ analysis }: { analysis: CopilotAnalysis }) {
  return <>
    <section className="analysis-hero panel">
      <div><span className="eyebrow">FINAL VIEW</span><h3>{analysis.verdict}</h3><p>{analysis.summary}</p></div>
      <div className="analysis-score"><Gauge size={19} /><strong>{analysis.score ?? '—'}</strong><small>/ 100</small></div>
    </section>
    <div className="argument-grid"><EvidenceCard kind="support" title="支持买入的证据" items={analysis.support} /><EvidenceCard kind="oppose" title="反对买入的证据" items={analysis.oppose} /></div>
    <div className="condition-panel panel">
      <div className="panel-heading"><div><span className="eyebrow">DECISION GATE</span><h2>买入前必须同时满足</h2></div><CircleDollarSign size={20} /></div>
      <ol>{analysis.buyConditions.map((condition) => <li key={condition}><span><Check size={14} /></span>{condition}</li>)}</ol>
      <div className="invalidation"><strong>失效条件</strong>{analysis.invalidation.map((item) => <span key={item}>{item}</span>)}</div>
    </div>
  </>
}

function FinancialPanel({ intelligence }: { intelligence: StockIntelligence }) {
  if (!intelligence.quote) return <UnavailablePanel icon={<Building2 size={22} />} title="财务与估值数据暂不可用" detail="当前没有带来源与报告期的正式财务数据。" />
  return <section className="financial-panel panel">
    <SectionHeading eyebrow="MARKET SNAPSHOT" title="演示市场指标" />
    <p className="panel-caveat"><AlertTriangle size={15} />这些指标来自 demo fixture，仅用于页面布局与交互验收，不是正式财务分析。</p>
    <MetricGrid intelligence={intelligence} />
  </section>
}

function MetricGrid({ intelligence }: { intelligence: StockIntelligence }) {
  const quote = intelligence.quote
  if (!quote) return null
  return <dl className="stock-metric-grid panel">
    <Metric label="开盘" value={`¥${quote.open.toFixed(2)}`} />
    <Metric label="日内范围" value={`¥${quote.low.toFixed(2)}–¥${quote.high.toFixed(2)}`} />
    <Metric label="前收" value={`¥${quote.previousClose.toFixed(2)}`} />
    <Metric label="成交量" value={`${(quote.volume / 10_000).toFixed(1)} 万`} />
    <Metric label="换手率" value={`${quote.turnoverRate.toFixed(2)}%`} />
    <Metric label="市盈率（TTM）" value={quote.peTtm?.toFixed(2) ?? '—'} />
    <Metric label="演示市值" value={formatMarketCap(quote.marketCapCny)} />
    <Metric label="行情来源" value="demo_fixture" />
  </dl>
}

function EventTimeline({ events, unavailable }: { events: StockEvent[]; unavailable: boolean }) {
  if (!events.length) return <div className="event-empty"><MessageSquareText size={20} /><p>{unavailable ? '当前没有按股票代码验证通过的公告、新闻或舆情数据。' : '当前窗口没有关联事件。'}</p></div>
  return <div className="event-timeline">{events.map((event) => <article key={event.id}><time>{formatDateTime(event.publishedAt)}</time><i className={event.sentiment} /><div><span>{eventKindLabel(event.kind)} · {event.source}</span><strong>{event.title}</strong><p>{event.summary}</p><small>关联：{event.relatedSymbols.join('、')} · {event.url ? '有来源链接' : '演示无外链'}</small></div></article>)}</div>
}

function EventCards({ events, unavailable }: { events: StockEvent[]; unavailable: boolean }) {
  if (!events.length) return <div className="event-empty"><Newspaper size={20} /><p>{unavailable ? '事件数据未交付；公告标题和舆情结论不会由模型自动补写。' : '当前没有关联内容。'}</p></div>
  return <div className="event-card-grid">{events.map((event) => <article key={event.id}><span className={`event-kind ${event.kind}`}>{eventKindLabel(event.kind)}</span><h3>{event.title}</h3><p>{event.summary}</p><small>{event.source} · {formatDateTime(event.publishedAt)}</small></article>)}</div>
}

function QuestionRows({ analysis }: { analysis: CopilotAnalysis }) {
  const count = Math.max(analysis.support.length, analysis.oppose.length, 1)
  return <div className="question-list">{Array.from({ length: count }, (_, index) => {
    const support = analysis.support[index]
    const oppose = analysis.oppose[index]
    return <article key={`${support?.title ?? 'support'}-${oppose?.title ?? 'oppose'}-${index}`}>
      <h3>{support?.title ?? oppose?.title ?? '当前分析完整度'}</h3>
      <div><section className="bull"><span>支持观点</span><p>{support?.detail ?? '当前没有更多可验证的支持证据。'}</p></section><section className="bear"><span>反对观点</span><p>{oppose?.detail ?? '反证数据仍不完整，不能因此视为没有风险。'}</p></section></div>
    </article>
  })}</div>
}

function EvidenceCard({ kind, title, items }: { kind: 'support' | 'oppose'; title: string; items: Array<{ title: string; detail: string }> }) {
  return <section className={`panel evidence-card ${kind}`}><div className="panel-heading"><h2>{title}</h2><span>{items.length}</span></div>{items.length ? items.map((item) => <article key={`${item.title}-${item.detail}`}><i>{kind === 'support' ? '+' : '−'}</i><div><strong>{item.title}</strong><p>{item.detail}</p></div></article>) : <div className="rail-empty">当前没有可验证证据。</div>}</section>
}

function UnavailablePanel({ icon, title, detail }: { icon: React.ReactNode; title: string; detail: string }) {
  return <section className="unavailable-tab panel">{icon}<div><strong>{title}</strong><p>{detail}</p><small>状态：analysis_unavailable · 不生成替代数据</small></div></section>
}

function SectionHeading({ eyebrow, title }: { eyebrow: string; title: string }) { return <div className="section-heading"><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div> }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div> }
function eventKindLabel(kind: StockEvent['kind']) { return kind === 'announcement' ? '公告' : kind === 'news' ? '新闻' : '舆情' }
function analysisModeLabel(mode: CopilotAnalysis['mode']) { return mode === 'tradingagent_observation' ? 'TradingAgent 观察' : mode === 'demo_fixture' ? '演示数据' : '暂无正式分析' }
function formatMarketCap(value: number) { return value >= 100_000_000_000 ? `¥${(value / 100_000_000_000).toFixed(2)} 千亿` : `¥${(value / 100_000_000).toFixed(0)} 亿` }
function formatDateTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
