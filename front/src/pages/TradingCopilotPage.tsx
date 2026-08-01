import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowLeftRight, Bell, BookOpenCheck, Building2, ChevronRight,
  Eye, Gauge, LayoutDashboard, ListPlus, Pencil, Plus, Search, ShieldCheck, Sparkles,
  MessageCircleMore, WalletCards, X,
} from 'lucide-react'
import { copilotDemoAnalyses, createCopilotDemoState, unavailableAnalysis } from '../copilot/demo'
import { analysisFromSignal } from '../copilot/analysis'
import { getDemoStockIntelligence, summarizeStockSentiment, unavailableStockIntelligence, type StockIntelligence } from '../copilot/stockIntelligence'
import { loadStockIntelligence } from '../copilot/stockIntelligenceClient'
import { loadTradingCopilotState, saveTradingCopilotState, type CopilotPersistence } from '../copilot/tradingCopilotClient'
import { isAshareSymbol, type CopilotAnalysis, type CopilotDecisionAction, type CopilotHolding, type TradingCopilotState } from '../copilot/types'
import { createTradingAgentSnapshotClient } from '../api/tradingAgentIntegration'
import { StockDetailWorkspace } from '../components/copilot/StockDetailWorkspace'
import '../styles/trading-copilot.css'

type Editor = 'account' | 'holding' | null

export function TradingCopilotPage({ demoPreviewEnabled, onOpenQuant }: { demoPreviewEnabled: boolean; onOpenQuant: () => void }) {
  const [state, setState] = useState<TradingCopilotState | null>(null)
  const [persistence, setPersistence] = useState<CopilotPersistence>('local_draft')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [editor, setEditor] = useState<Editor>(null)
  const [watchOpen, setWatchOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [usingDemoSeed, setUsingDemoSeed] = useState(false)
  const [tradingAgentAnalyses, setTradingAgentAnalyses] = useState<Record<string, CopilotAnalysis>>({})
  const [formalStockIntelligence, setFormalStockIntelligence] = useState<Record<string, StockIntelligence>>({})

  useEffect(() => {
    const previous = document.title
    document.title = 'TradingCopilot · A股人工决策台'
    return () => { document.title = previous }
  }, [])

  useEffect(() => {
    let active = true
    void loadTradingCopilotState().then((loaded) => {
      if (!active) return
      const empty = loaded.state.watchlist.length === 0 && loaded.state.holdings.length === 0 && loaded.state.account.declaredCapitalCny === 0
      const next = empty && demoPreviewEnabled ? createCopilotDemoState() : loaded.state
      setUsingDemoSeed(empty && demoPreviewEnabled)
      setState(next)
      setSelectedSymbol(next.watchlist[0]?.symbol ?? '')
      setPersistence(loaded.persistence)
    })
    return () => { active = false }
  }, [demoPreviewEnabled])

  useEffect(() => {
    let active = true
    const client = createTradingAgentSnapshotClient({ timeoutMs: 4000 })
    async function refreshAnalyses() {
      try {
        const snapshot = await client.getSnapshot()
        if (!active) return
        setTradingAgentAnalyses(Object.fromEntries(snapshot.signals
          .filter((signal) => signal.market === 'A-share' && isAshareSymbol(signal.symbol))
          .map((signal) => [signal.symbol, analysisFromSignal(signal, snapshot.generatedAt)])))
      } catch {
        if (active) setTradingAgentAnalyses({})
      }
    }
    void refreshAnalyses()
    const timer = window.setInterval(() => void refreshAnalyses(), 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    if (!selectedSymbol) return
    let active = true
    void loadStockIntelligence(selectedSymbol).then((projection) => {
      if (!active || !projection) return
      setFormalStockIntelligence((current) => ({ ...current, [selectedSymbol]: projection }))
    })
    return () => { active = false }
  }, [selectedSymbol])

  const selected = state?.watchlist.find((item) => item.symbol === selectedSymbol) ?? state?.watchlist[0]
  const selectedFormalIntelligence = selected ? formalStockIntelligence[selected.symbol] : undefined
  const analysis = selected
    ? tradingAgentAnalyses[selected.symbol]
      ?? (!selectedFormalIntelligence && demoPreviewEnabled ? copilotDemoAnalyses[selected.symbol] : undefined)
      ?? unavailableAnalysis(selected.symbol, selected.name)
    : unavailableAnalysis('------.--', '未选择股票')
  const intelligence = selected
    ? selectedFormalIntelligence
      ?? (demoPreviewEnabled && analysis.mode === 'demo_fixture' ? getDemoStockIntelligence(selected.symbol) : null)
      ?? unavailableStockIntelligence(selected.symbol, selected.name)
    : unavailableStockIntelligence(analysis.symbol, analysis.name)
  const holding = state?.holdings.find((item) => item.symbol === selected?.symbol)
  const latestDecision = state?.decisions.findLast((item) => item.symbol === selected?.symbol)
  const investedCost = useMemo(() => state?.holdings.reduce((sum, item) => sum + item.quantity * item.averageCost, 0) ?? 0, [state?.holdings])

  async function commit(transform: (current: TradingCopilotState) => TradingCopilotState, message: string) {
    if (!state) return
    const now = new Date().toISOString()
    const next = { ...transform(state), updatedAt: now }
    setState(next)
    setUsingDemoSeed(false)
    setPersistence(await saveTradingCopilotState(next))
    setNotice(message)
    window.setTimeout(() => setNotice(''), 2800)
  }

  async function addWatchItem() {
    if (!state) return
    const parsed = parseWatchQuery(query)
    if (!parsed || !isAshareSymbol(parsed.symbol)) {
      setNotice('请输入“000001.SZ 平安银行”或六位股票代码')
      return
    }
    if (state.watchlist.some((item) => item.symbol === parsed.symbol)) {
      setSelectedSymbol(parsed.symbol)
      setNotice('已切换到这只股票')
      return
    }
    const now = new Date().toISOString()
    await commit((current) => ({ ...current, watchlist: [...current.watchlist, { ...parsed, addedAt: now }] }), '已加入关注；暂无证据时不会生成建议')
    setSelectedSymbol(parsed.symbol)
    setQuery('')
  }

  async function recordDecision(action: CopilotDecisionAction) {
    if (!selected) return
    const recordedAt = new Date().toISOString()
    await commit((current) => ({
      ...current,
      decisions: [{
        id: window.crypto.randomUUID(), symbol: selected.symbol, action, recordedAt,
        actor: current.ownerId, authority: 'human_intent_only' as const,
      }, ...current.decisions].slice(0, 200),
    }), `已记录：${decisionLabel(action)}（未下单）`)
  }

  if (!state) return <main className="copilot-loading"><Activity className="spin" />正在读取你的 Copilot 状态…</main>

  return (
    <main className="copilot-shell">
      <aside className="copilot-sidebar">
        <button className="copilot-brand" type="button" aria-label="TradingCopilot 首页">
          <span className="brand-mark"><Sparkles size={17} /></span>
          <span><strong>Trading</strong><b>Copilot</b><small>A 股人工决策台</small></span>
        </button>
        <nav aria-label="TradingCopilot 导航">
          <button className="active" type="button"><LayoutDashboard size={17} />决策台</button>
          <button type="button" onClick={() => setWatchOpen(true)}><Eye size={17} />关注列表</button>
          <button type="button" onClick={() => setEditor('holding')}><WalletCards size={17} />资金与持仓</button>
          <button type="button" onClick={() => document.querySelector('.decision-history')?.scrollIntoView()}><BookOpenCheck size={17} />决策记录</button>
        </nav>
        <div className="copilot-boundary">
          <ShieldCheck size={18} />
          <div><strong>人工确认边界</strong><p>Copilot 只记录计划，不连接券商、不自动下单。</p></div>
        </div>
        <button className="quant-switch" onClick={onOpenQuant} type="button"><ArrowLeftRight size={16} />打开量化运行台</button>
      </aside>

      <section className="copilot-main">
        <header className="copilot-topbar">
          <div><span className="eyebrow">PERSONAL A-SHARE DESK</span><h1>今天先看条件，再做决定</h1></div>
          <div className="copilot-top-actions">
            <span className={`persistence ${usingDemoSeed ? 'demo' : persistence}`}>{usingDemoSeed ? '演示预览 · 首次修改后保存' : persistence === 'server' ? '已保存到个人状态账本' : '本机草稿'}</span>
            <button aria-label="提醒" className="round-button" type="button"><Bell size={17} /></button>
            <span className="avatar">N</span>
          </div>
        </header>

        <section className="account-strip" aria-label="用户申报账户摘要">
          <div><span>申报总资金</span><strong>{money(state.account.declaredCapitalCny)}</strong><small>用户手工维护 · 非券商确认</small></div>
          <div><span>可用现金</span><strong>{money(state.account.availableCashCny)}</strong><small>{ratio(state.account.availableCashCny, state.account.declaredCapitalCny)} 现金</small></div>
          <div><span>持仓成本</span><strong>{money(investedCost)}</strong><small>{state.holdings.length} 只申报持仓</small></div>
          <div><span>关注股票</span><strong>{state.watchlist.length}<em> 只</em></strong><small>{state.decisions.length} 条人工决策记录</small></div>
          <button className="secondary-button" onClick={() => setEditor('account')} type="button"><Pencil size={15} />调整资金</button>
        </section>

        <div className="copilot-grid">
          {watchOpen ? <button aria-label="关闭关注列表" className="watch-backdrop" onClick={() => setWatchOpen(false)} type="button" /> : null}
          <aside aria-label="我的关注列表" className={`copilot-watch panel ${watchOpen ? 'open' : ''}`}>
            <div className="panel-heading"><div><span className="eyebrow">WATCHLIST</span><h2>我的关注</h2></div><div className="watch-heading-actions"><span>{state.watchlist.length}</span><button aria-label="收起关注列表" onClick={() => setWatchOpen(false)} type="button"><X size={14} /></button></div></div>
            <div className="watch-search">
              <Search size={15} />
              <input aria-label="输入股票代码和名称" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void addWatchItem() }} placeholder="000001.SZ 平安银行" value={query} />
              <button aria-label="加入关注" onClick={() => void addWatchItem()} type="button"><Plus size={16} /></button>
            </div>
            <div className="watch-list">
              {state.watchlist.map((item) => {
                const itemAnalysis = tradingAgentAnalyses[item.symbol] ?? (demoPreviewEnabled ? copilotDemoAnalyses[item.symbol] : undefined) ?? unavailableAnalysis(item.symbol, item.name)
                return (
                  <button className={item.symbol === selected?.symbol ? 'selected' : ''} key={item.symbol} onClick={() => { setSelectedSymbol(item.symbol); setWatchOpen(false) }} type="button">
                    <span><strong>{item.name}</strong><small>{item.symbol}</small></span>
                    <span className="watch-score">{itemAnalysis.score ?? '--'}<small>{shortVerdict(itemAnalysis.verdict)}</small></span>
                    <ChevronRight size={15} />
                  </button>
                )
              })}
              {!state.watchlist.length && <div className="empty-watch"><ListPlus size={22} /><p>输入一只 A 股，建立你的观察清单。</p></div>}
            </div>
          </aside>

          <StockDetailWorkspace
            analysis={analysis}
            holding={holding}
            intelligence={intelligence}
            latestDecision={latestDecision ? decisionLabel(latestDecision.action) : '尚未记录'}
            onRecordDecision={(action) => void recordDecision(action)}
          />

          <aside className="copilot-rail">
            <CompanyProfileCard intelligence={intelligence} />
            <div className="panel recommendation-card">
              <span className="eyebrow">EVIDENCE CONSENSUS</span><h2>Copilot 证据共识</h2>
              <div className="recommendation-value"><Gauge size={19} /><strong>{analysis.score ?? '—'}</strong><small>/100</small></div>
              <div className="score-track" aria-label={`建议强度 ${analysis.score ?? '暂无'}`}><i style={{ width: `${analysis.score ?? 0}%` }} /></div>
              <div className="evidence-consensus" aria-label={`支持 ${analysis.support.length} 条，反对 ${analysis.oppose.length} 条`}>
                <span><i className="support" />{analysis.support.length} 支持</span>
                <span><i className="oppose" />{analysis.oppose.length} 反对</span>
              </div>
              <strong className="recommendation-verdict">{analysis.verdict}</strong>
              <p>这是可追溯证据的人工辅助评分，不是分析师共识、胜率、收益承诺或自动下单指令。</p>
            </div>
            <SentimentPulseCard intelligence={intelligence} />
            <div className="panel holding-card">
              <div className="panel-heading"><div><span className="eyebrow">POSITION</span><h2>持仓关系</h2></div><button aria-label="编辑持仓" onClick={() => setEditor('holding')} type="button"><Pencil size={14} /></button></div>
              {holding ? <>
                <dl><div><dt>持有数量</dt><dd>{holding.quantity.toLocaleString('zh-CN')} 股</dd></div><div><dt>可卖数量</dt><dd>{holding.sellableQuantity.toLocaleString('zh-CN')} 股</dd></div><div><dt>平均成本</dt><dd>¥{holding.averageCost.toFixed(2)}</dd></div><div><dt>申报市值</dt><dd>{money(holding.quantity * holding.averageCost)}</dd></div></dl>
                <p>仅按你的成本申报计算，不代表实时市值。</p>
              </> : <div className="rail-empty">这只股票不在你的申报持仓中。</div>}
            </div>
            <div className="panel source-card">
              <span className="eyebrow">SOURCE & FRESHNESS</span><h2>证据来源</h2>
              <div className={`source-state ${analysis.mode}`}><i />{analysisModeLabel(analysis.mode)}</div>
              <p>{analysis.mode === 'demo_fixture' ? '仅用于本轮界面与交互验收，不应据此交易。' : analysis.mode === 'analysis_unavailable' ? 'TradingAgent 尚未交付该标的的可验证观察。' : '来自 TradingAgent 的带时间只读观察。'}</p>
              <small>{analysis.generatedAt ? formatTime(analysis.generatedAt) : '无分析时间'}</small>
            </div>
            <div className="panel decision-history">
              <span className="eyebrow">RECENT INTENT</span><h2>最近记录</h2>
              {state.decisions.slice(0, 4).map((item) => <div className="history-row" key={item.id}><span>{item.symbol}</span><strong>{decisionLabel(item.action)}</strong><small>{formatTime(item.recordedAt)}</small></div>)}
              {!state.decisions.length && <div className="rail-empty">还没有人工决策记录。</div>}
            </div>
          </aside>
        </div>
      </section>

      {editor === 'account' && <AccountEditor account={state.account} onClose={() => setEditor(null)} onSave={(account) => { void commit((current) => ({ ...current, account }), '资金信息已更新'); setEditor(null) }} />}
      {editor === 'holding' && <HoldingEditor holding={holding} symbol={selected?.symbol ?? ''} name={selected?.name ?? ''} onClose={() => setEditor(null)} onSave={(nextHolding) => { void commit((current) => ({ ...current, holdings: [...current.holdings.filter((item) => item.symbol !== nextHolding.symbol), nextHolding] }), '申报持仓已更新'); setEditor(null) }} />}
      {notice && <div className="copilot-toast" role="status">{notice}</div>}
    </main>
  )
}

function CompanyProfileCard({ intelligence }: { intelligence: StockIntelligence }) {
  const company = intelligence.company
  return <section className="panel company-profile-card">
    <div className="rail-card-title"><Building2 size={15} /><span>公司资料</span></div>
    {company ? <>
      <dl>
        <div><dt>代码</dt><dd>{intelligence.symbol}</dd></div>
        <div><dt>上市日期</dt><dd>{company.listingDate}</dd></div>
        <div><dt>行业</dt><dd>{company.industry}</dd></div>
        <div><dt>地区</dt><dd>{company.area}</dd></div>
        <div><dt>交易所</dt><dd>{company.exchange === 'SH' ? '上交所' : '深交所'}</dd></div>
      </dl>
      <p>{company.description}</p>
    </> : <div className="rail-empty">公司资料尚未随正式个股投影交付。</div>}
  </section>
}

function SentimentPulseCard({ intelligence }: { intelligence: StockIntelligence }) {
  const summary = summarizeStockSentiment(intelligence.events)
  const denominator = summary.total || 1
  return <section className="panel sentiment-pulse-card">
    <div className="rail-card-title"><MessageCircleMore size={15} /><span>舆论与事件温度</span></div>
    <div className="sentiment-tone"><strong>{summary.tone}</strong><small>{summary.total ? `${summary.total} 条已关联事件` : '暂无可验证事件'}</small></div>
    <div className="sentiment-track" aria-label={`积极 ${summary.positive}，中性 ${summary.neutral}，谨慎 ${summary.negative}`}>
      <i className="positive" style={{ width: `${summary.positive / denominator * 100}%` }} />
      <i className="neutral" style={{ width: `${summary.neutral / denominator * 100}%` }} />
      <i className="negative" style={{ width: `${summary.negative / denominator * 100}%` }} />
    </div>
    <div className="sentiment-counts"><span>积极 {summary.positive}</span><span>中性 {summary.neutral}</span><span>谨慎 {summary.negative}</span></div>
    <p>仅统计按股票代码关联的公告、新闻与舆论方向；热度不能替代价格条件和风险复核。</p>
    <small>{summary.latestPublishedAt ? `最近更新 ${formatTime(summary.latestPublishedAt)}` : '无事件时间'}</small>
  </section>
}

function AccountEditor({ account, onClose, onSave }: { account: TradingCopilotState['account']; onClose: () => void; onSave: (account: TradingCopilotState['account']) => void }) {
  const [capital, setCapital] = useState(String(account.declaredCapitalCny))
  const [cash, setCash] = useState(String(account.availableCashCny))
  return <EditorShell title="调整申报资金" subtitle="这是你手工维护的个人账户视图，不会改变量化账户。" onClose={onClose}><label>申报总资金（元）<input autoFocus min="0" onChange={(event) => setCapital(event.target.value)} type="number" value={capital} /></label><label>可用现金（元）<input min="0" onChange={(event) => setCash(event.target.value)} type="number" value={cash} /></label><button className="primary-button" disabled={Number(cash) > Number(capital)} onClick={() => onSave({ declaredCapitalCny: Number(capital), availableCashCny: Number(cash), updatedAt: new Date().toISOString() })} type="button">保存资金信息</button></EditorShell>
}

function HoldingEditor({ holding, symbol, name, onClose, onSave }: { holding?: CopilotHolding; symbol: string; name: string; onClose: () => void; onSave: (holding: CopilotHolding) => void }) {
  const [quantity, setQuantity] = useState(String(holding?.quantity ?? 0))
  const [sellable, setSellable] = useState(String(holding?.sellableQuantity ?? 0))
  const [cost, setCost] = useState(String(holding?.averageCost ?? 0))
  return <EditorShell title={`更新持仓 · ${name || '先选择股票'}`} subtitle={`${symbol || '未选择'} · 用户申报，非券商确认`} onClose={onClose}><label>持有数量（股）<input autoFocus min="0" onChange={(event) => setQuantity(event.target.value)} step="100" type="number" value={quantity} /></label><label>可卖数量（股）<input min="0" onChange={(event) => setSellable(event.target.value)} step="100" type="number" value={sellable} /></label><label>平均成本（元）<input min="0" onChange={(event) => setCost(event.target.value)} step="0.01" type="number" value={cost} /></label><button className="primary-button" disabled={!isAshareSymbol(symbol) || Number(sellable) > Number(quantity)} onClick={() => onSave({ symbol, name, quantity: Number(quantity), sellableQuantity: Number(sellable), averageCost: Number(cost), updatedAt: new Date().toISOString() })} type="button">保存申报持仓</button></EditorShell>
}

function EditorShell({ title, subtitle, onClose, children }: { title: string; subtitle: string; onClose: () => void; children: React.ReactNode }) {
  return <div className="editor-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}><section aria-modal="true" className="copilot-editor" role="dialog"><button aria-label="关闭" className="editor-close" onClick={onClose} type="button"><X size={18} /></button><span className="eyebrow">USER-DECLARED STATE</span><h2>{title}</h2><p>{subtitle}</p><div className="editor-fields">{children}</div></section></div>
}

function parseWatchQuery(value: string) {
  const [rawCode, ...nameParts] = value.trim().split(/\s+/)
  if (!rawCode) return null
  let symbol = rawCode.toUpperCase()
  if (/^\d{6}$/.test(symbol)) symbol = `${symbol}.${symbol.startsWith('6') ? 'SH' : 'SZ'}`
  const known = copilotDemoAnalyses[symbol]
  return { symbol, name: nameParts.join(' ') || known?.name || symbol.slice(0, 6) }
}

function money(value: number) { return `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` }
function ratio(value: number, total: number) { return total ? `${Math.round((value / total) * 100)}%` : '0%' }
function formatTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
function decisionLabel(action: CopilotDecisionAction) { return ({ planned: '加入计划', observing: '继续观察', skipped: '暂不交易' })[action] }
function shortVerdict(value: string) { return value.replace('条件', '').replace('参与', '') }
function analysisModeLabel(mode: string) { return ({ demo_fixture: '演示分析', tradingagent_observation: 'TA 正式观察', analysis_unavailable: '暂无正式分析' } as Record<string, string>)[mode] ?? mode }
