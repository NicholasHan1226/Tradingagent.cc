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

type CopilotView = 'desk' | 'watchlist' | 'portfolio' | 'decisions'
type HoldingTarget = Pick<CopilotHolding, 'symbol' | 'name'>

export function TradingCopilotPage({ demoPreviewEnabled, onOpenQuant }: { demoPreviewEnabled: boolean; onOpenQuant: () => void }) {
  const [state, setState] = useState<TradingCopilotState | null>(null)
  const [persistence, setPersistence] = useState<CopilotPersistence>('local_draft')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [view, setView] = useState<CopilotView>('desk')
  const [accountEditorOpen, setAccountEditorOpen] = useState(false)
  const [holdingTarget, setHoldingTarget] = useState<HoldingTarget | null>(null)
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [holdingQuery, setHoldingQuery] = useState('')
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
      const next = ensureHoldingsWatched(demoPreviewEnabled ? createCopilotDemoState() : loaded.state)
      setUsingDemoSeed(demoPreviewEnabled)
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
  const hasDeclaredState = Boolean(state && (state.account.declaredCapitalCny > 0 || state.account.availableCashCny > 0 || state.holdings.length > 0 || state.watchlist.length > 0 || state.decisions.length > 0))

  async function commit(transform: (current: TradingCopilotState) => TradingCopilotState, message: string) {
    if (!state) return
    const now = new Date().toISOString()
    const next = { ...transform(state), updatedAt: now }
    setState(next)
    if (!usingDemoSeed) setPersistence(await saveTradingCopilotState(next))
    setNotice(usingDemoSeed ? `${message}（演示修改未保存）` : message)
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

  function beginAddHolding() {
    const parsed = parseWatchQuery(holdingQuery)
    if (!parsed || !isAshareSymbol(parsed.symbol)) {
      setNotice('请输入“000001.SZ 平安银行”或六位股票代码')
      return
    }
    setHoldingTarget(parsed)
  }

  async function saveHolding(nextHolding: CopilotHolding) {
    const now = new Date().toISOString()
    await commit((current) => ({
      ...current,
      holdings: [...current.holdings.filter((item) => item.symbol !== nextHolding.symbol), nextHolding],
      watchlist: current.watchlist.some((item) => item.symbol === nextHolding.symbol)
        ? current.watchlist
        : [...current.watchlist, { symbol: nextHolding.symbol, name: nextHolding.name, addedAt: now }],
    }), '申报持仓已更新，并已纳入关注')
    setSelectedSymbol(nextHolding.symbol)
    setHoldingTarget(null)
    setHoldingQuery('')
  }

  async function removeHolding(symbol: string) {
    await commit((current) => ({ ...current, holdings: current.holdings.filter((item) => item.symbol !== symbol) }), '已移除申报持仓；股票仍保留在关注列表')
    setHoldingTarget(null)
  }

  async function removeWatchItem(symbol: string) {
    if (!state) return
    if (state.holdings.some((item) => item.symbol === symbol)) {
      setNotice('持仓股票必须保留在关注列表；请先在“资金与持仓”移除持仓')
      return
    }
    await commit((current) => ({ ...current, watchlist: current.watchlist.filter((item) => item.symbol !== symbol) }), '已移除关注')
    if (selectedSymbol === symbol) setSelectedSymbol(state.watchlist.find((item) => item.symbol !== symbol)?.symbol ?? '')
  }

  function openStock(symbol: string) {
    setSelectedSymbol(symbol)
    setView('desk')
    window.scrollTo({ top: 0, behavior: 'smooth' })
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
        <button className="copilot-brand" onClick={() => setView('desk')} type="button" aria-label="TradingCopilot 首页">
          <span className="brand-mark"><Sparkles size={17} /></span>
          <span><strong>Trading</strong><b>Copilot</b><small>A 股人工决策台</small></span>
        </button>
        <nav aria-label="TradingCopilot 导航">
          <button className={view === 'desk' ? 'active' : ''} onClick={() => setView('desk')} type="button"><LayoutDashboard size={17} />决策台</button>
          <button className={view === 'watchlist' ? 'active' : ''} onClick={() => setView('watchlist')} type="button"><Eye size={17} />关注列表</button>
          <button className={view === 'portfolio' ? 'active' : ''} onClick={() => setView('portfolio')} type="button"><WalletCards size={17} />资金与持仓</button>
          <button className={view === 'decisions' ? 'active' : ''} onClick={() => setView('decisions')} type="button"><BookOpenCheck size={17} />决策记录</button>
        </nav>
        <div className="copilot-boundary">
          <ShieldCheck size={18} />
          <div><strong>人工确认边界</strong><p>Copilot 只记录计划，不连接券商、不自动下单。</p></div>
        </div>
        <button className="quant-switch" onClick={onOpenQuant} type="button"><ArrowLeftRight size={16} />打开量化运行台</button>
      </aside>

      <section className="copilot-main">
        <header className="copilot-topbar">
          <div><span className="eyebrow">PERSONAL A-SHARE DESK</span><h1>{viewTitle(view)}</h1></div>
          <div className="copilot-top-actions">
            <span className={`persistence ${usingDemoSeed ? 'demo' : persistence}`}>{usingDemoSeed ? '独立演示样例 · 修改不会保存' : hasDeclaredState ? persistence === 'server' ? '已保存到个人状态账本' : '本机草稿' : '尚未录入个人状态'}</span>
            <button aria-label="提醒功能尚未启用" className="round-button" disabled title="提醒功能尚未启用" type="button"><Bell size={17} /></button>
            <span className="avatar">N</span>
          </div>
        </header>

        <section className="account-strip" aria-label="用户申报账户摘要">
          <div><span>申报总资金</span><strong>{money(state.account.declaredCapitalCny)}</strong><small>用户手工维护 · 非券商确认</small></div>
          <div><span>可用现金</span><strong>{money(state.account.availableCashCny)}</strong><small>{ratio(state.account.availableCashCny, state.account.declaredCapitalCny)} 现金</small></div>
          <div><span>持仓成本</span><strong>{money(investedCost)}</strong><small>{state.holdings.length} 只申报持仓</small></div>
          <div><span>关注股票</span><strong>{state.watchlist.length}<em> 只</em></strong><small>{state.decisions.length} 条人工决策记录</small></div>
          <button className="secondary-button" onClick={() => setAccountEditorOpen(true)} type="button"><Pencil size={15} />调整资金</button>
        </section>

        {view === 'watchlist' && <WatchlistWorkspace analyses={tradingAgentAnalyses} demoPreviewEnabled={demoPreviewEnabled} holdings={state.holdings} onAdd={() => void addWatchItem()} onOpen={openStock} onRemove={(symbol) => void removeWatchItem(symbol)} query={query} setQuery={setQuery} watchlist={state.watchlist} />}
        {view === 'portfolio' && <PortfolioWorkspace account={state.account} holdings={state.holdings} investedCost={investedCost} holdingQuery={holdingQuery} onAdd={beginAddHolding} onEdit={(item) => setHoldingTarget(item)} onOpen={openStock} setHoldingQuery={setHoldingQuery} />}
        {view === 'decisions' && <DecisionWorkspace decisions={state.decisions} onOpen={openStock} />}

        {view === 'desk' && !selected && <EmptyDesk onPortfolio={() => setView('portfolio')} onWatchlist={() => setView('watchlist')} />}
        {view === 'desk' && selected && <div className="copilot-grid">
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
              <div className="panel-heading"><div><span className="eyebrow">CURRENT STOCK POSITION</span><h2>当前个股持仓</h2></div><button aria-label="编辑当前个股持仓" onClick={() => setHoldingTarget(selected)} type="button"><Pencil size={14} /></button></div>
              {holding ? <>
                <dl><div><dt>持有数量</dt><dd>{holding.quantity.toLocaleString('zh-CN')} 股</dd></div><div><dt>可卖数量</dt><dd>{holding.sellableQuantity.toLocaleString('zh-CN')} 股</dd></div><div><dt>平均成本</dt><dd>¥{holding.averageCost.toFixed(2)}</dd></div><div><dt>申报市值</dt><dd>{money(holding.quantity * holding.averageCost)}</dd></div></dl>
                <p>仅按你的成本申报计算，不代表实时市值。</p>
              </> : <div className="rail-empty">这只股票不在你的申报持仓中。</div>}
              <button className="rail-link" onClick={() => setView('portfolio')} type="button">查看全部 {state.holdings.length} 只持仓 <ChevronRight size={13} /></button>
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
        </div>}
      </section>

      {accountEditorOpen && <AccountEditor account={state.account} onClose={() => setAccountEditorOpen(false)} onSave={(account) => { void commit((current) => ({ ...current, account }), '资金信息已更新'); setAccountEditorOpen(false) }} />}
      {holdingTarget && <HoldingEditor holding={state.holdings.find((item) => item.symbol === holdingTarget.symbol)} symbol={holdingTarget.symbol} name={holdingTarget.name} onClose={() => setHoldingTarget(null)} onRemove={state.holdings.some((item) => item.symbol === holdingTarget.symbol) ? () => void removeHolding(holdingTarget.symbol) : undefined} onSave={(nextHolding) => void saveHolding(nextHolding)} />}
      {notice && <div className="copilot-toast" role="status">{notice}</div>}
    </main>
  )
}

function WatchlistWorkspace({ analyses, demoPreviewEnabled, holdings, onAdd, onOpen, onRemove, query, setQuery, watchlist }: {
  analyses: Record<string, CopilotAnalysis>
  demoPreviewEnabled: boolean
  holdings: CopilotHolding[]
  onAdd: () => void
  onOpen: (symbol: string) => void
  onRemove: (symbol: string) => void
  query: string
  setQuery: (value: string) => void
  watchlist: TradingCopilotState['watchlist']
}) {
  return <section className="management-workspace" aria-label="关注列表管理">
    <div className="management-heading">
      <div><span className="eyebrow">WATCHLIST MANAGEMENT</span><h2>关注列表</h2><p>关注不等于持仓。持仓股票会自动进入关注；仅关注股票不会被计入资产。</p></div>
      <span className="management-count">{watchlist.length} 只关注 · {holdings.length} 只持仓</span>
    </div>
    <div className="management-search">
      <Search size={16} />
      <input aria-label="输入股票代码和名称" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') onAdd() }} placeholder="000001.SZ 平安银行" value={query} />
      <button className="primary-button" onClick={onAdd} type="button"><Plus size={15} />加入关注</button>
    </div>
    <div className="management-table panel">
      <div className="management-table-head"><span>股票</span><span>账户关系</span><span>证据判断</span><span>强度</span><span>操作</span></div>
      {watchlist.map((item) => {
        const itemAnalysis = analyses[item.symbol] ?? (demoPreviewEnabled ? copilotDemoAnalyses[item.symbol] : undefined) ?? unavailableAnalysis(item.symbol, item.name)
        const held = holdings.some((holdingItem) => holdingItem.symbol === item.symbol)
        return <div className="management-row" key={item.symbol}>
          <button className="stock-cell" onClick={() => onOpen(item.symbol)} type="button"><strong>{item.name}</strong><small>{item.symbol}</small></button>
          <span><i className={held ? 'relation held' : 'relation'}>{held ? '持仓' : '仅关注'}</i></span>
          <span>{itemAnalysis.verdict}</span>
          <strong className="management-score">{itemAnalysis.score ?? '—'}</strong>
          <span className="row-actions"><button onClick={() => onOpen(item.symbol)} type="button">打开个股</button><button disabled={held} onClick={() => onRemove(item.symbol)} title={held ? '持仓股票须保留关注' : '移除关注'} type="button">移除</button></span>
        </div>
      })}
      {!watchlist.length && <div className="management-empty"><ListPlus size={25} /><strong>还没有关注股票</strong><p>先输入一只 A 股；系统不会自动把演示股票当成你的关注。</p></div>}
    </div>
  </section>
}

function PortfolioWorkspace({ account, holdings, investedCost, holdingQuery, onAdd, onEdit, onOpen, setHoldingQuery }: {
  account: TradingCopilotState['account']
  holdings: CopilotHolding[]
  investedCost: number
  holdingQuery: string
  onAdd: () => void
  onEdit: (holding: CopilotHolding) => void
  onOpen: (symbol: string) => void
  setHoldingQuery: (value: string) => void
}) {
  return <section className="management-workspace" aria-label="资金与持仓管理">
    <div className="management-heading">
      <div><span className="eyebrow">USER-DECLARED PORTFOLIO</span><h2>资金与持仓</h2><p>这里展示全部手工申报持仓，不是当前股票的编辑器，也不是券商实时对账。</p></div>
      <span className="management-count">{holdings.length} 只申报持仓</span>
    </div>
    <div className="portfolio-checks">
      <div><span>申报总资金</span><strong>{money(account.declaredCapitalCny)}</strong></div>
      <div><span>可用现金</span><strong>{money(account.availableCashCny)}</strong></div>
      <div><span>持仓成本合计</span><strong>{money(investedCost)}</strong></div>
      <div className="reconcile-pending"><span>券商资产核对</span><strong>待人工确认</strong><small>缺少实时市值，不用成本倒推差额</small></div>
    </div>
    <div className="management-search">
      <Search size={16} />
      <input aria-label="输入新增持仓股票代码和名称" onChange={(event) => setHoldingQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') onAdd() }} placeholder="000001.SZ 平安银行" value={holdingQuery} />
      <button className="primary-button" onClick={onAdd} type="button"><Plus size={15} />新增持仓</button>
    </div>
    <div className="management-table panel">
      <div className="portfolio-table-head"><span>股票</span><span>持有 / 可卖</span><span>平均成本</span><span>成本金额</span><span>更新时间</span><span>操作</span></div>
      {holdings.map((item) => <div className="portfolio-row" key={item.symbol}>
        <button className="stock-cell" onClick={() => onOpen(item.symbol)} type="button"><strong>{item.name}</strong><small>{item.symbol}</small></button>
        <span>{item.quantity.toLocaleString('zh-CN')} / {item.sellableQuantity.toLocaleString('zh-CN')} 股</span>
        <span>¥{item.averageCost.toFixed(2)}</span>
        <strong>{money(item.quantity * item.averageCost)}</strong>
        <small>{formatTime(item.updatedAt)}</small>
        <span className="row-actions"><button onClick={() => onOpen(item.symbol)} type="button">打开个股</button><button onClick={() => onEdit(item)} type="button">编辑 / 移除</button></span>
      </div>)}
      {!holdings.length && <div className="management-empty"><WalletCards size={25} /><strong>尚未申报持仓</strong><p>请逐只录入真实数量、可卖数量和成本；系统不会从演示样例推断你的资产。</p></div>}
    </div>
    <p className="management-footnote">A 股 T+1 的可卖数量由你申报；数量和成本仍需与券商账户人工核对。申报总资金不与持仓成本强行勾稽，因为缺少实时市值时两者不可直接比较。新增持仓会自动加入关注列表，移除持仓不会自动取消关注。</p>
  </section>
}

function DecisionWorkspace({ decisions, onOpen }: { decisions: TradingCopilotState['decisions']; onOpen: (symbol: string) => void }) {
  return <section className="management-workspace" aria-label="人工决策记录">
    <div className="management-heading"><div><span className="eyebrow">HUMAN INTENT LEDGER</span><h2>决策记录</h2><p>这里只是人工意图账本，不是委托、成交或量化样本。</p></div><span className="management-count">{decisions.length} 条记录</span></div>
    <div className="management-table panel">
      <div className="decision-table-head"><span>时间</span><span>股票</span><span>人工决定</span><span>权限</span><span>操作</span></div>
      {decisions.map((item) => <div className="decision-row" key={item.id}><small>{formatTime(item.recordedAt)}</small><strong>{item.symbol}</strong><span>{decisionLabel(item.action)}</span><span>仅记录意图</span><button onClick={() => onOpen(item.symbol)} type="button">打开个股</button></div>)}
      {!decisions.length && <div className="management-empty"><BookOpenCheck size={25} /><strong>还没有人工决策记录</strong><p>只有你在个股页明确记录后，这里才会出现；不会把系统建议当成你的决定。</p></div>}
    </div>
  </section>
}

function EmptyDesk({ onPortfolio, onWatchlist }: { onPortfolio: () => void; onWatchlist: () => void }) {
  return <section className="empty-desk">
    <div className="empty-desk-card panel"><Sparkles size={28} /><span className="eyebrow">START WITH YOUR OWN FACTS</span><h2>先录入真实关注和持仓</h2><p>当前没有个人状态。开发环境不会再自动把任何演示样例当成你的账户。</p><div><button className="primary-button" onClick={onWatchlist} type="button">建立关注列表</button><button className="secondary-button" onClick={onPortfolio} type="button">录入全部持仓</button></div></div>
  </section>
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

function HoldingEditor({ holding, symbol, name, onClose, onRemove, onSave }: { holding?: CopilotHolding; symbol: string; name: string; onClose: () => void; onRemove?: () => void; onSave: (holding: CopilotHolding) => void }) {
  const [quantity, setQuantity] = useState(String(holding?.quantity ?? 0))
  const [sellable, setSellable] = useState(String(holding?.sellableQuantity ?? 0))
  const [cost, setCost] = useState(String(holding?.averageCost ?? 0))
  return <EditorShell title={`${holding ? '更新' : '新增'}持仓 · ${name || '先选择股票'}`} subtitle={`${symbol || '未选择'} · 用户申报，非券商确认`} onClose={onClose}><label>持有数量（股）<input autoFocus min="0" onChange={(event) => setQuantity(event.target.value)} step="100" type="number" value={quantity} /></label><label>可卖数量（股）<input min="0" onChange={(event) => setSellable(event.target.value)} step="100" type="number" value={sellable} /></label><label>平均成本（元）<input min="0" onChange={(event) => setCost(event.target.value)} step="0.01" type="number" value={cost} /></label><div className="editor-actions">{onRemove && <button className="danger-button" onClick={onRemove} type="button">移除申报持仓</button>}<button className="primary-button" disabled={!isAshareSymbol(symbol) || Number(quantity) <= 0 || Number(cost) < 0 || Number(sellable) > Number(quantity)} onClick={() => onSave({ symbol, name, quantity: Number(quantity), sellableQuantity: Number(sellable), averageCost: Number(cost), updatedAt: new Date().toISOString() })} type="button">保存申报持仓</button></div></EditorShell>
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

function ensureHoldingsWatched(state: TradingCopilotState): TradingCopilotState {
  const missing = state.holdings.filter((holding) => !state.watchlist.some((item) => item.symbol === holding.symbol))
  if (!missing.length) return state
  return {
    ...state,
    watchlist: [...state.watchlist, ...missing.map((holding) => ({ symbol: holding.symbol, name: holding.name, addedAt: holding.updatedAt }))],
  }
}

function money(value: number) { return `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` }
function ratio(value: number, total: number) { return total ? `${Math.round((value / total) * 100)}%` : '0%' }
function formatTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
function decisionLabel(action: CopilotDecisionAction) { return ({ planned: '加入计划', observing: '继续观察', skipped: '暂不交易' })[action] }
function viewTitle(view: CopilotView) { return ({ desk: '今天先看条件，再做决定', watchlist: '先分清关注与持仓', portfolio: '核对完整资金与持仓', decisions: '复盘你的人工决定' })[view] }
function analysisModeLabel(mode: string) { return ({ demo_fixture: '演示分析', tradingagent_observation: 'TA 正式观察', analysis_unavailable: '暂无正式分析' } as Record<string, string>)[mode] ?? mode }
