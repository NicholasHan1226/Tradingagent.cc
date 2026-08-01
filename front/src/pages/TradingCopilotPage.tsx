import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowLeftRight, Bell, BookOpenCheck, Check, ChevronRight, CircleDollarSign,
  Eye, Gauge, LayoutDashboard, ListPlus, Pencil, Plus, Search, ShieldCheck, Sparkles,
  WalletCards, X,
} from 'lucide-react'
import { copilotDemoAnalyses, createCopilotDemoState, unavailableAnalysis } from '../copilot/demo'
import { analysisFromSignal } from '../copilot/analysis'
import { loadTradingCopilotState, saveTradingCopilotState, type CopilotPersistence } from '../copilot/tradingCopilotClient'
import { isAshareSymbol, type CopilotAnalysis, type CopilotDecisionAction, type CopilotHolding, type TradingCopilotState } from '../copilot/types'
import { createTradingAgentSnapshotClient } from '../api/tradingAgentIntegration'
import '../styles/trading-copilot.css'

type Editor = 'account' | 'holding' | null

export function TradingCopilotPage({ demoPreviewEnabled, onOpenQuant }: { demoPreviewEnabled: boolean; onOpenQuant: () => void }) {
  const [state, setState] = useState<TradingCopilotState | null>(null)
  const [persistence, setPersistence] = useState<CopilotPersistence>('local_draft')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [editor, setEditor] = useState<Editor>(null)
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [usingDemoSeed, setUsingDemoSeed] = useState(false)
  const [tradingAgentAnalyses, setTradingAgentAnalyses] = useState<Record<string, CopilotAnalysis>>({})

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

  const selected = state?.watchlist.find((item) => item.symbol === selectedSymbol) ?? state?.watchlist[0]
  const analysis = selected
    ? tradingAgentAnalyses[selected.symbol] ?? (demoPreviewEnabled ? copilotDemoAnalyses[selected.symbol] : undefined) ?? unavailableAnalysis(selected.symbol, selected.name)
    : unavailableAnalysis('------.--', '未选择股票')
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
          <button type="button" onClick={() => document.querySelector('.copilot-watch')?.scrollIntoView()}><Eye size={17} />关注列表</button>
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
          <aside className="copilot-watch panel">
            <div className="panel-heading"><div><span className="eyebrow">WATCHLIST</span><h2>我的关注</h2></div><span>{state.watchlist.length}</span></div>
            <div className="watch-search">
              <Search size={15} />
              <input aria-label="输入股票代码和名称" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void addWatchItem() }} placeholder="000001.SZ 平安银行" value={query} />
              <button aria-label="加入关注" onClick={() => void addWatchItem()} type="button"><Plus size={16} /></button>
            </div>
            <div className="watch-list">
              {state.watchlist.map((item) => {
                const itemAnalysis = tradingAgentAnalyses[item.symbol] ?? (demoPreviewEnabled ? copilotDemoAnalyses[item.symbol] : undefined) ?? unavailableAnalysis(item.symbol, item.name)
                return (
                  <button className={item.symbol === selected?.symbol ? 'selected' : ''} key={item.symbol} onClick={() => setSelectedSymbol(item.symbol)} type="button">
                    <span><strong>{item.name}</strong><small>{item.symbol}</small></span>
                    <span className="watch-score">{itemAnalysis.score ?? '--'}<small>{shortVerdict(itemAnalysis.verdict)}</small></span>
                    <ChevronRight size={15} />
                  </button>
                )
              })}
              {!state.watchlist.length && <div className="empty-watch"><ListPlus size={22} /><p>输入一只 A 股，建立你的观察清单。</p></div>}
            </div>
          </aside>

          <section className="stock-workspace">
            <div className="stock-hero panel">
              <div className="stock-title">
                <span className="eyebrow">{analysis.symbol}</span>
                <h2>{analysis.name}</h2>
                <span className={`source-badge ${analysis.mode}`}>{analysisModeLabel(analysis.mode)}</span>
              </div>
              <div className="stock-meta">
                <div><span>当前状态</span><strong>{analysis.verdict}</strong></div>
                <div><span>你的持仓</span><strong>{holding ? `${holding.quantity.toLocaleString('zh-CN')} 股` : '未持有'}</strong></div>
                <div><span>最近决定</span><strong>{latestDecision ? decisionLabel(latestDecision.action) : '尚未记录'}</strong></div>
              </div>
              <p className="analysis-summary">{analysis.summary}</p>
              <div className="score-row">
                <span><Gauge size={16} />建议强度</span>
                <div className="score-track" aria-label={`建议强度 ${analysis.score ?? '暂无'}`}><i style={{ width: `${analysis.score ?? 0}%` }} /></div>
                <strong>{analysis.score === null ? '--' : `${analysis.score}/100`}</strong>
              </div>
              <p className="score-caption">强度用于组织证据，不是胜率、收益承诺或自动下单指令。</p>
            </div>

            <div className="argument-grid">
              <EvidenceCard kind="support" title="支持买入的证据" items={analysis.support} />
              <EvidenceCard kind="oppose" title="反对买入的证据" items={analysis.oppose} />
            </div>

            <div className="condition-panel panel">
              <div className="panel-heading"><div><span className="eyebrow">DECISION GATE</span><h2>买入前必须同时满足</h2></div><CircleDollarSign size={20} /></div>
              <ol>{analysis.buyConditions.map((condition) => <li key={condition}><span><Check size={14} /></span>{condition}</li>)}</ol>
              <div className="invalidation"><strong>失效条件</strong>{analysis.invalidation.map((item) => <span key={item}>{item}</span>)}</div>
            </div>

            <div className="decision-bar">
              <div><strong>你的决定</strong><span>只写入人工决策账本，不会触发任何订单</span></div>
              <button className="ghost-button" onClick={() => void recordDecision('skipped')} type="button">暂不交易</button>
              <button className="secondary-button" onClick={() => void recordDecision('observing')} type="button">继续观察</button>
              <button className="primary-button" disabled={analysis.mode === 'analysis_unavailable'} onClick={() => void recordDecision('planned')} type="button">加入人工计划</button>
            </div>
          </section>

          <aside className="copilot-rail">
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

function EvidenceCard({ kind, title, items }: { kind: 'support' | 'oppose'; title: string; items: Array<{ title: string; detail: string }> }) {
  return <section className={`panel evidence-card ${kind}`}><div className="panel-heading"><h2>{title}</h2><span>{items.length}</span></div>{items.length ? items.map((item) => <article key={`${item.title}-${item.detail}`}><i>{kind === 'support' ? '+' : '−'}</i><div><strong>{item.title}</strong><p>{item.detail}</p></div></article>) : <div className="rail-empty">当前没有可验证证据。</div>}</section>
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
