import { useMemo, useState } from 'react'
import {
  AlertTriangle, Building2, CalendarDays, Check, CircleDollarSign, FileText, Gauge, MessageSquareText,
  Newspaper, ShieldAlert, Sparkles, TrendingDown, TrendingUp,
} from 'lucide-react'
import type { CopilotAnalysis, CopilotDecisionAction, CopilotHolding } from '../../copilot/types'
import type { ForecastReadinessStatus } from '../../copilot/forecastReadiness'
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

export function StockDetailWorkspace({ analysis, intelligence, holding, latestDecision, decisionDisabled = false, onRecordDecision }: {
  analysis: CopilotAnalysis
  intelligence: StockIntelligence
  holding?: CopilotHolding
  latestDecision: string
  decisionDisabled?: boolean
  onRecordDecision: (action: CopilotDecisionAction) => void
}) {
  const [activeTab, setActiveTab] = useState<StockDetailTab>('overview')
  const [range, setRange] = useState<StockRange>('1D')
  const [showForecast, setShowForecast] = useState(false)
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
    <MarketRulesStrip intelligence={intelligence} />

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
      <div><strong>你的决定</strong><span>{decisionDisabled ? '研究界面预览不会写入个人决策账本' : '只写入人工决策账本，不会触发任何订单'}</span></div>
      <button className="ghost-button" disabled={decisionDisabled} onClick={() => onRecordDecision('skipped')} type="button">暂不交易</button>
      <button className="secondary-button" disabled={decisionDisabled} onClick={() => onRecordDecision('observing')} type="button">继续观察</button>
      <button className="primary-button" disabled={decisionDisabled || analysis.readiness.action !== 'eligible_for_human_review'} onClick={() => onRecordDecision('planned')} title={analysis.readiness.action !== 'eligible_for_human_review' ? '只有正式、定型且通过门禁的证据可进入人工计划' : undefined} type="button">加入人工计划</button>
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
      <SectionHeading eyebrow="PRICE & EVENT CONTEXT" title="显著价格变化" />
      <EventTimeline events={intelligence.events} intelligence={intelligence} unavailable={intelligence.mode === 'analysis_unavailable'} />
    </section>
    <section className="story-analysis panel">
      <SectionHeading eyebrow="STORIES & ANALYSIS" title="公告、新闻与舆论" />
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
      <div><ShieldAlert size={18} /><span><strong>{readinessLabel(forecast.readiness.status)}</strong><small>预测期限：{forecast.horizonLabel}</small></span></div>
      <p>{forecast.caveat}</p>
    </section>
    <StockMarketChart intelligence={intelligence} range={range} showForecast={showForecast} onRangeChange={onRangeChange} onToggleForecast={onToggleForecast} />
    <section className="forecast-grid">
      <div className="panel scenario-card">
        <SectionHeading eyebrow="DIRECTIONAL SCENARIO" title="方向研究情景" />
        <div className={`directional-view ${forecast.directionalView === '偏强' ? 'up' : forecast.directionalView === '偏弱' ? 'down' : 'neutral'}`}>
          {forecast.directionalView === '偏强' ? <TrendingUp size={17} /> : forecast.directionalView === '偏弱' ? <TrendingDown size={17} /> : <Gauge size={17} />}
          <span>当前定性结果</span><strong>{forecast.directionalView}</strong>
        </div>
        <p>{forecast.takeaway}</p>
      </div>
      <div className="panel driver-card">
        <SectionHeading eyebrow="DELIVERY GATES" title="预测交付门禁" />
        <ul className="forecast-gate-list">{forecast.readiness.gates.map((gate) => <li className={gate.passed ? 'passed' : 'blocked'} key={gate.id}>{gate.passed ? <Check size={14} /> : <AlertTriangle size={14} />}{gate.label}</li>)}</ul>
        <small>只有全部通过，页面才允许显示概率与覆盖率标签。</small>
      </div>
    </section>
    <section className="model-boundary panel">
      <div><strong>当前模型</strong><span>{modelLabel(forecast.modelId)}</span></div>
      <div><strong>Kronos 角色</strong><span>Challenger · 同门禁对照</span></div>
      <p>Kronos 不直接合并进最终建议；只有在同一 PIT 输入、冻结样本外、费用口径和校准门禁下稳定超过线性基线，才进入人工评审。</p>
    </section>
  </>
}

function HistoryPanel({ intelligence, range, onRangeChange }: { intelligence: StockIntelligence; range: StockRange; onRangeChange: (range: StockRange) => void }) {
  const rows = useMemo(() => intelligence.series[range].filter((point) => point.price !== null).slice(-12).reverse(), [intelligence.series, range])
  if (!rows.length) return <UnavailablePanel icon={<FileText size={22} />} title="历史数据暂不可用" detail="没有可验证的个股价格序列。" />
  const dataType = intelligence.verification.status === 'verified' ? '正式投影' : intelligence.mode === 'demo_fixture' ? '演示数据' : '不可用'
  return <section className="history-table-panel panel">
    <div className="history-table-heading"><SectionHeading eyebrow="PRICE HISTORY" title="历史数据" /><select aria-label="历史数据周期" onChange={(event) => onRangeChange(event.target.value as StockRange)} value={range}>{(['1D', '5D', '1M', '6M', 'YTD', '1Y'] as StockRange[]).map((item) => <option key={item}>{item}</option>)}</select></div>
    <div className="history-table-wrap"><table><thead><tr><th>时间</th><th>价格</th><th>成交量</th><th>数据类型</th></tr></thead><tbody>{rows.map((row) => <tr key={row.key}><td>{row.label}</td><td>¥{row.price?.toFixed(2)}</td><td>{row.volume?.toLocaleString('zh-CN') ?? '—'}</td><td><span className={intelligence.verification.status === 'verified' ? 'formal-data-label' : 'demo-data-label'}>{dataType}</span></td></tr>)}</tbody></table></div>
  </section>
}

function AnalysisPanel({ analysis }: { analysis: CopilotAnalysis }) {
  return <>
    <section className="analysis-hero panel">
      <div><span className="eyebrow">FINAL VIEW</span><h3>{analysis.verdict}</h3><p>{analysis.summary}</p></div>
      <div className="analysis-score"><Gauge size={19} /><strong>{analysis.evidenceStrength.value ?? '—'}</strong><small>{analysis.evidenceStrength.value === null ? analysis.evidenceStrength.label : '/ 100'}</small></div>
    </section>
    <DecisionReadiness analysis={analysis} />
    <div className="argument-grid"><EvidenceCard kind="support" title="支持买入的证据" items={analysis.support} /><EvidenceCard kind="oppose" title="反对买入的证据" items={analysis.oppose} /></div>
    <div className="condition-panel panel">
      <div className="panel-heading"><div><span className="eyebrow">DECISION GATE</span><h2>买入前必须同时满足</h2></div><CircleDollarSign size={20} /></div>
      <ol>{analysis.buyConditions.map((condition) => <li key={condition}><span><Check size={14} /></span>{condition}</li>)}</ol>
      <div className="invalidation"><strong>失效条件</strong>{analysis.invalidation.map((item) => <span key={item}>{item}</span>)}</div>
    </div>
  </>
}

function FinancialPanel({ intelligence }: { intelligence: StockIntelligence }) {
  return <UnavailablePanel icon={<Building2 size={22} />} title="财务报表尚未接入" detail={`当前${intelligence.verification.status === 'verified' ? '正式个股投影仅含行情与事件' : '页面'}没有带报告期、披露日、修订链和来源回执的财务报表合同；行情指标不冒充财务数据。`} />
}

function MetricGrid({ intelligence }: { intelligence: StockIntelligence }) {
  const quote = intelligence.quote
  if (!quote) return null
  return <dl className="stock-metric-grid panel">
    <Metric label="开盘" value={`¥${quote.open.toFixed(2)}`} />
    <Metric label="日内范围" value={`¥${quote.low.toFixed(2)}–¥${quote.high.toFixed(2)}`} />
    <Metric label="前收" value={`¥${quote.previousClose.toFixed(2)}`} />
    <Metric label="成交量" value={`${(quote.volume / 10_000).toFixed(1)} 万`} />
    <Metric label="换手率" value={quote.turnoverRate === null ? '未交付' : `${quote.turnoverRate.toFixed(2)}%`} />
    <Metric label="市盈率（TTM）" value={quote.peTtm?.toFixed(2) ?? '—'} />
    <Metric label={intelligence.mode === 'demo_fixture' ? '演示市值' : '市值'} value={quote.marketCapCny === null ? '未交付' : formatMarketCap(quote.marketCapCny)} />
    <Metric label="行情来源" value={intelligence.source ? `${intelligence.source.datasetId}/${intelligence.source.freshness}` : intelligence.mode} />
  </dl>
}

function EventTimeline({ events, intelligence, unavailable }: { events: StockEvent[]; intelligence: StockIntelligence; unavailable: boolean }) {
  if (!events.length) return <div className="event-empty"><MessageSquareText size={20} /><p>{unavailable ? '当前没有按股票代码验证通过的公告、新闻或舆情数据。' : '当前窗口没有关联事件。'}</p></div>
  return <div className="event-timeline">
    {intelligence.quote ? <article className="price-move-summary"><time>{formatDateTime(intelligence.updatedAt ?? '')}</time><i className={intelligence.quote.change >= 0 ? 'positive' : 'negative'} /><div><span>价格变化 · {intelligence.mode === 'demo_fixture' ? '演示行情' : '正式个股投影'}</span><strong>¥{intelligence.quote.price.toFixed(2)} · {signedPercent(intelligence.quote.changePct)}</strong><p>相对前收 ¥{intelligence.quote.previousClose.toFixed(2)}；以下事件仅作为关联解释，不单独形成交易理由。</p></div></article> : null}
    {events.map((event) => <article key={event.id}><time>{formatDateTime(event.publishedAt)}</time><i className={event.sentiment} /><div><span>{eventKindLabel(event.kind)} · {event.source} · 来源置信 {percent(event.sourceConfidence)}</span><strong>{event.title}</strong><p>{event.summary}</p><small>关联：{event.relatedSymbols.join('、')} · {noveltyLabel(event.novelty)} · {impactLabel(event)}</small><EventMeta event={event} /></div></article>)}
  </div>
}

function EventCards({ events, unavailable }: { events: StockEvent[]; unavailable: boolean }) {
  if (!events.length) return <div className="event-empty"><Newspaper size={20} /><p>{unavailable ? '事件数据未交付；公告标题和舆情结论不会由模型自动补写。' : '当前没有关联内容。'}</p></div>
  return <div className="event-card-grid">{events.map((event) => <article key={event.id}><span className={`event-kind ${event.kind}`}>{eventKindLabel(event.kind)}</span><h3>{event.title}</h3><p>{event.summary}</p><small>{event.sourceClass} · 来源置信 {percent(event.sourceConfidence)} · 情绪置信 {percent(event.sentimentConfidence)}</small><EventMeta event={event} />{event.url ? <a href={event.url} rel="noreferrer" target="_blank">查看来源</a> : null}</article>)}</div>
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

function DecisionReadiness({ analysis }: { analysis: CopilotAnalysis }) {
  const items = [
    ['数据', decisionReadinessLabel(analysis.readiness.data)],
    ['证据', decisionReadinessLabel(analysis.readiness.evidence)],
    ['模型', decisionReadinessLabel(analysis.readiness.model)],
    ['人工决策', decisionReadinessLabel(analysis.readiness.action)],
  ]
  return <section className="decision-readiness panel"><SectionHeading eyebrow="DECISION READINESS" title="四层就绪度" /><div>{items.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}</div>{analysis.readiness.reasons.length ? <ul>{analysis.readiness.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}</section>
}

function MarketRulesStrip({ intelligence }: { intelligence: StockIntelligence }) {
  const rules = intelligence.marketRules
  if (!rules) return <div className="market-rules-strip blocked"><ShieldAlert size={15} /><span>A 股交易规则未随正式投影交付，人工计划入口保持阻断。</span></div>
  return <div className="market-rules-strip" aria-label="A股交易约束"><ShieldAlert size={15} /><span>{boardLabel(rules.board)}</span><span>T+1</span><span>{rules.lotSize} 股一手</span><span>{rules.priceLimitPct === null ? '涨跌停未知' : `涨跌停 ±${rules.priceLimitPct}%`}</span><span>{rules.stStatus === 'normal' ? '非 ST' : rules.stStatus.toUpperCase()}</span><span>{rules.tradingStatus === 'trading' ? '可交易' : rules.tradingStatus === 'suspended' ? '停牌' : '交易状态未知'}</span><span>{rules.corporateActionAdjusted === true ? '已复权' : rules.corporateActionAdjusted === false ? '未复权' : '复权口径未知'}</span></div>
}

function EventMeta({ event }: { event: StockEvent }) {
  return <div className="event-meta"><span>发布 {formatDateTime(event.publishedAt)}</span><span>采集 {formatDateTime(event.retrievedAt)}</span>{event.revisedAt ? <span>修订 {formatDateTime(event.revisedAt)}</span> : null}<span>{event.url ? '已绑定来源链接' : '无外链'}</span>{event.sourceReceiptId ? <span title={event.sourceReceiptSha256 ?? undefined}>回执 {event.sourceReceiptId}</span> : <span>无正式回执</span>}{event.dataCapability ? <span title={`as_of ${event.dataCapability.asOf} · lineage ${event.dataCapability.lineageSha256}`}>{event.dataCapability.datasetId} · {event.dataCapability.freshness}</span> : null}</div>
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
function signedPercent(value: number) { return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` }
function percent(value: number | null | StockEvent['sourceConfidence']) {
  if (typeof value === 'number') return `${Math.round(value * 100)}%`
  return ({ high: '高', medium: '中', low: '低', demo: '演示' } as Record<string, string>)[value ?? ''] ?? '未知'
}
function impactLabel(event: StockEvent) { return `影响 ${({ positive: '偏正面', negative: '偏负面', neutral: '中性', uncertain: '不确定' } as Record<string, string>)[event.impactDirection]} · ${({ intraday: '日内', short_term: '短期', medium_term: '中期', unknown: '期限未知' } as Record<string, string>)[event.impactHorizon]}` }
function noveltyLabel(value: StockEvent['novelty']) { return ({ new: '首次出现', updated: '内容更新', repeated: '重复信息' } as Record<string, string>)[value] }
function boardLabel(board: NonNullable<StockIntelligence['marketRules']>['board']) { return ({ main: '主板', gem: '创业板', star: '科创板', beijing: '北交所', unknown: '板块未知' } as Record<string, string>)[board] }
function decisionReadinessLabel(value: string) { return ({ verified: '已验证', demo: '演示', unavailable: '不可用', typed: '已定型', unscored_observation: '未评分观察', ready: '已就绪', blocked: '阻断', not_applicable: '不适用', eligible_for_human_review: '可供人工复核', observe_only: '仅观察' } as Record<string, string>)[value] ?? value }
function readinessLabel(status: ForecastReadinessStatus) {
  return status === 'decision_support_ready' ? '预测门禁已通过' : status === 'illustrative_only' ? '研究演示 · 概率停显' : '预测已阻断'
}
function modelLabel(modelId: 'naive_last_value' | 'linear_ridge_baseline' | 'kronos_challenger') {
  return ({ naive_last_value: '最后值基线', linear_ridge_baseline: '线性 / 岭回归基线', kronos_challenger: 'Kronos Challenger' })[modelId]
}
