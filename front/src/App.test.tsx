import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources } from './api/tradingAgentReadModel'
import App from './App'

describe('App navigation and result-first dashboard', () => {
  beforeEach(() => {
    vi.spyOn(window, 'setInterval').mockImplementation(() => 0 as unknown as ReturnType<typeof window.setInterval>)
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  function click(element: HTMLElement) {
    act(() => {
      fireEvent.click(element)
    })
  }

  it('renders the homepage around return, funnel, and chart without system wording', () => {
    render(<App />)

    expect(screen.getByLabelText('实时收益')).toBeInTheDocument()
    expect(screen.getByLabelText('机会管道')).toBeInTheDocument()
    expect(screen.getAllByText('收益曲线').length).toBeGreaterThan(0)
    expect(within(screen.getByLabelText('实时收益')).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'true')
    expect(within(screen.getByLabelText('实时收益')).getByRole('tab', { name: '真实账户' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('信号').length).toBeGreaterThan(0)
    expect(screen.queryByRole('tablist', { name: '收益区间' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '机会从全市场进入，只把可执行结果留在首页。' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '正在推进' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '本轮结果' })).not.toBeInTheDocument()
    expect(screen.queryByText('现在判断')).not.toBeInTheDocument()
    expect(screen.queryByText('看决策')).not.toBeInTheDocument()
    expect(screen.queryByText('总览')).not.toBeInTheDocument()
  })

  it('replaces demo signals with TradingAgent snapshot signals when the local API is available', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-04T10:00:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [
              {
                symbol: '0700.HK',
                name: '腾讯',
                market: 'HK',
                method: '事件驱动',
                status: 'pending',
                impact: '--',
                confidence: '86%',
                age: '31m',
                reason: '价格和成交量接近走强',
                next: '等待触发条件',
                steps: 5,
              },
              {
                symbol: 'BTC-USD',
                name: 'Bitcoin',
                market: 'Crypto',
                method: '波动突破',
                status: 'missed',
                impact: '-4.3',
                confidence: '62%',
                age: '3h',
                reason: '风险过高',
                next: '进入复盘',
                steps: 5,
              },
            ],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText(/2 个进入 · 2 个留下 · 0 个成交/)).toBeInTheDocument())
    expect(screen.getAllByText('BTC-USD').length).toBeGreaterThan(0)
  })

  it('does not replace an empty TradingAgent snapshot with demo results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-04T10:00:00.000Z',
            domains: {
              performance: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              signals: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              risk: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText('等待收益结果')).toBeInTheDocument())
    expect(screen.getAllByText('暂无机会结果').length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: '等待机会' })).toBeInTheDocument()
    expect(screen.queryByText(/等待新机会 · 转化 0%/)).not.toBeInTheDocument()
    expect(screen.getByText('暂无持仓记录')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('shows A-share account facts and strategy sample quality in the return cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-06T13:10:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              holdings: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              decisions: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
            },
            performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
            portfolio: {
              pnlAmount: -65,
              returnPct: -0.03,
              capitalBase: 200000,
              targetPct: 8,
              maxDrawdownPct: 0,
              tradeCount: 13,
              pointCount: 1,
              source: 'shared/logs/local_sim/local_sim_trades.jsonl',
              pnlSource: 'ashare_local_sim_account',
              pnlCurrency: 'CNY',
              realizedPnl: 0,
              unrealizedPnl: -65,
              updatedAt: '2026-07-06T13:10:00.000Z',
              ashareAccount: {
                cashAvailable: 101397.47,
                marketValue: 98537.53,
                accountEquity: 199935,
                accountTotalPnl: -65,
                accountReturnPct: -0.03,
                openPositionCount: 13,
                totalSampleCount: 13,
                validationSampleCount: 13,
                strategySampleValidCount: 0,
                strategyTotalPnl: 0,
                strategyMarketValue: 0,
                strategyOpenPositionCount: 0,
                source: 'shared/logs/local_sim/local_sim_trades.jsonl',
                updatedAt: '2026-07-06T13:10:00.000Z',
              },
            },
            holdings: [{ symbol: '000001.SZ', name: '000001.SZ', market: 'A-share', weight: '¥7,206', pnl: '-¥5', risk: '正常', role: '模拟盘持仓' }],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('总资产')).not.toHaveLength(0))
    expect(screen.getAllByText('机会管道').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓跟踪').length).toBeGreaterThan(0)
    expect(screen.getAllByText('继续跟进').length).toBeGreaterThan(0)
    expect(screen.getAllByText('暂无新机会').length).toBeGreaterThan(0)
    expect(screen.queryByText(/0 个新机会 · 1 个持仓在跟踪 · 转化/)).not.toBeInTheDocument()
    expect(screen.queryByText(/暂无新信号进入/)).not.toBeInTheDocument()
    expect(screen.getAllByText('¥19.99万').length).toBeGreaterThan(0)
    expect(screen.getAllByText('复盘收益').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/可复盘/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('0/13').length).toBeGreaterThan(0)
    expect(screen.queryByText(/可复盘 0\/13 · 链路验证 13/)).not.toBeInTheDocument()
  })

  it('shows return range controls on the returns page only', () => {
    render(<App />)

    expect(screen.queryByRole('tablist', { name: '收益区间' })).not.toBeInTheDocument()
    click(screen.getByRole('button', { name: '收益' }))

    const rangeSwitch = screen.getByRole('tablist', { name: '收益区间' })
    expect(within(rangeSwitch).getByRole('tab', { name: '今日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '7日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '30日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '全部' })).toBeInTheDocument()
  })

  it('switches the dashboard to market-specific signals, holdings, and summaries', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-06T13:10:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              holdings: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              decisions: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
            },
            performance: [{ day: '现在', simulated: 0.4, target: 8, benchmark: 0, opportunity: 0 }],
            holdings: [
              { symbol: '600519.SH', name: '贵州茅台', market: 'A-share', weight: '¥7,206', pnl: '-¥5', risk: '正常', role: '模拟盘持仓' },
              { symbol: 'BTC-USD', name: 'BTC-USD', market: 'Crypto', weight: '$1,200', pnl: '+$18', risk: '正常', role: 'Grid 持仓' },
            ],
            signals: [
              {
                symbol: '600519.SH',
                name: '贵州茅台',
                market: 'A-share',
                method: '候选池',
                status: 'pending',
                impact: '--',
                confidence: '--',
                age: '4m',
                reason: '等待确认',
                next: '继续观察',
                steps: 3,
                capitalEvidence: {
                  score: 0.82,
                  netInflow: 12800000,
                  source: 'signal_scores',
                },
              },
              {
                symbol: 'BTC-USD',
                name: 'BTC-USD',
                market: 'Crypto',
                method: 'Grid · 买入',
                status: 'executed',
                impact: '成交 $667',
                confidence: '已成交',
                age: '2m',
                reason: '模拟盘成交',
                next: '进入复盘',
                steps: 6,
                stage: '成交',
                stageEvidence: 'replay',
              },
            ],
            funnelEvents: [],
            marketSummaries: [
              {
                market: 'A-share',
                status: 'ready',
                runtimeState: 'strategy_wait',
                executionFault: false,
                holdingCount: 1,
                signalCount: 1,
                tradeCount: 0,
                styleCount: 4,
                activeStyleCount: 3,
                noTradeEvidence: {
                  category: 'capital_plan_defensive',
                  evidenceStatus: 'ready',
                  evidenceGaps: [],
                  universeCount: 3213,
                  candidateCount: 3,
                  orderCount: 0,
                  capitalPlanCapacity: 0,
                  riskMode: 'defensive',
                  allowedBuyCount: 0,
                  strategyCashAvailable: 200000,
                  accountCashAvailable: 82683.89,
                  strategyPositionCount: 0,
                  accountPositionCount: 2,
                  ignoredValidationSampleCount: 2,
                },
                source: 'shared/runtime_test/sim_market_health_latest.json',
                headline: 'A股模拟盘策略等待',
                detail: '无交易：capital_plan_defensive',
              },
              {
                market: 'Crypto',
                status: 'ready',
                runtimeState: 'strategy_wait',
                executionFault: false,
                holdingCount: 1,
                signalCount: 1,
                tradeCount: 1,
                styleCount: 2,
                activeStyleCount: 2,
                pnlAmount: 18,
                returnPct: 0.18,
                maxDrawdownPct: 0.4,
                source: 'shared/review/*/style_comparison.json',
                headline: '加密已有 1 笔模拟成交',
                detail: '收益 +18 · 回报 +0.18% · 风格 2/2',
              },
            ],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('BTC-USD').length).toBeGreaterThan(0))
    expect(screen.getByLabelText('市场运行状态')).toBeInTheDocument()
    expect(screen.getByText('个股流向')).toBeInTheDocument()
    expect(screen.getByText('资金分 82')).toBeInTheDocument()
    expect(screen.getByText('净流入 +¥1280.00万')).toBeInTheDocument()
    click(screen.getByRole('button', { name: '全市场' }))
    click(screen.getByRole('menuitem', { name: /A股/ }))

    expect(screen.getByLabelText('A股资金状态')).toBeInTheDocument()
    expect(screen.getByText('可用资金')).toBeInTheDocument()
    expect(screen.getByText('¥20.00万')).toBeInTheDocument()
    expect(screen.getByText('账户现金')).toBeInTheDocument()
    expect(screen.getByText('¥8.27万')).toBeInTheDocument()
    expect(screen.getByText('复盘/账户持仓')).toBeInTheDocument()
    expect(screen.getByText('0/2')).toBeInTheDocument()
    expect(screen.getByText('不计入复盘')).toBeInTheDocument()

    click(screen.getByRole('button', { name: 'A股' }))
    click(screen.getByRole('menuitem', { name: /加密/ }))

    expect(screen.getAllByText('等待机会').length).toBeGreaterThan(0)
    expect(screen.getByText('加密正在等更好的入场条件')).toBeInTheDocument()
    expect(screen.getAllByText('BTC-USD').length).toBeGreaterThan(0)
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('renders snapshot funnel events as a real trading flow', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-05T10:00:00.000Z',
            domains: {
              performance: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-05T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-05T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [
              {
                symbol: '600519.SH',
                name: '贵州茅台',
                market: 'A-share',
                method: '趋势跟踪',
                status: 'executed',
                impact: '+8.2',
                confidence: '81%',
                age: '12m',
                reason: '模拟成交',
                next: '持仓复盘',
                steps: 6,
                stage: '成交',
                stageEvidence: 'full',
              },
              {
                symbol: 'BTC-USD',
                name: 'Bitcoin',
                market: 'Crypto',
                method: '波动突破',
                status: 'cancelled',
                impact: '-3.1',
                confidence: '58%',
                age: '20m',
                reason: '风险拒绝',
                next: '放弃',
                steps: 3,
                stage: '拒绝',
                stageEvidence: 'partial',
              },
            ],
            funnelEvents: [
              { id: 'a', symbol: '600519.SH', market: 'A-share', stage: '发现', status: '进入', label: '机会进入', source: 'signal_queue' },
              { id: 'b', symbol: '600519.SH', market: 'A-share', stage: '研判', status: '通过', label: '研究通过', source: 'signal_queue' },
              { id: 'c', symbol: '600519.SH', market: 'A-share', stage: '风控', status: '通过', label: '风控通过', source: 'signal_queue' },
              { id: 'd', symbol: '600519.SH', market: 'A-share', stage: '队列', status: '等待', label: '待执行', source: 'signal_queue' },
              { id: 'e', symbol: '600519.SH', market: 'A-share', stage: '结果', status: '成交', label: '成交', source: 'sim_ledger' },
              { id: 'f', symbol: 'BTC-USD', market: 'Crypto', stage: '结果', status: '拦截', label: '放弃', source: 'signal_queue' },
            ],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText('实时')).toBeInTheDocument())
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('初筛').length).toBeGreaterThan(0)
    expect(screen.getAllByText('研究').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('信号').length).toBeGreaterThan(0)
    expect(screen.getAllByText('成交').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放弃').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('最近管道事件')).toBeInTheDocument()
    expect(screen.getAllByText('600519.SH').length).toBeGreaterThan(0)
  })

  it('switches the return card between simulated and reserved live mode in place', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    click(within(card).getByRole('tab', { name: '真实账户' }))

    expect(within(card).getByRole('tab', { name: '真实账户' })).toHaveAttribute('aria-selected', 'true')
    expect(within(card).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'false')
    expect(within(card).getAllByText('实盘未接入').length).toBeGreaterThan(0)
    expect(within(card).getByText('接入真实账户后，会在这里切换为实盘收益和风险边界。')).toBeInTheDocument()
  })

  it('shows actionable opportunity summary before the opportunity table', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '机会' }))

    expect(screen.getByRole('heading', { name: '当前可处理机会' })).toBeInTheDocument()
    expect(screen.getByText('可处理机会')).toBeInTheDocument()
    expect(screen.getByText('预期机会')).toBeInTheDocument()
    expect(screen.getByText('BTC-USD')).toBeInTheDocument()
    expect(screen.getByText('IF2601.CFFEX')).toBeInTheDocument()
  })

  it('keeps the reserved live state inside the return card', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    click(within(card).getByRole('tab', { name: '真实账户' }))

    expect(within(card).getAllByText('实盘未接入').length).toBeGreaterThan(0)
    expect(screen.queryByRole('dialog', { name: '实盘接入状态' })).not.toBeInTheDocument()
  })

  it('links a chart event marker to the related decision view', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '查看 5月28日 决策' }))

    expect(screen.getByText('决策影响收益')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '从机会到结果' })).toBeInTheDocument()
  })

  it('renders decision formation as a funnel with drop-off rates', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '决策' }))

    expect(screen.getAllByText('机会通过').length).toBeGreaterThan(0)
    expect(screen.getByText('未通过 33.3%')).toBeInTheDocument()
    expect(screen.getByText('33% 已兑现')).toBeInTheDocument()
  })
})
