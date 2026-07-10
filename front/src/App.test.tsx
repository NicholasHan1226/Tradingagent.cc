import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources } from './api/tradingAgentReadModel'
import App from './App'

describe('App navigation and result-first dashboard', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    vi.spyOn(window, 'setInterval').mockImplementation(() => 0 as unknown as ReturnType<typeof window.setInterval>)
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  function click(element: HTMLElement) {
    act(() => {
      fireEvent.click(element)
    })
  }

  it('uses six result-and-process destinations without decision pages', () => {
    render(<App />)

    const navigation = screen.getByRole('navigation', { name: '主导航' })
    expect(within(navigation).getAllByRole('button')).toHaveLength(6)
    expect(within(navigation).getByRole('button', { name: '总览' })).toBeInTheDocument()
    expect(within(navigation).getByRole('button', { name: '过程' })).toBeInTheDocument()
    expect(within(navigation).queryByRole('button', { name: '机会' })).not.toBeInTheDocument()
    expect(within(navigation).queryByRole('button', { name: '决策' })).not.toBeInTheDocument()

    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    expect(within(marketHeader).getByText('运行中').parentElement).toHaveTextContent('1')
    expect(within(marketHeader).getByText('已完成').parentElement).toHaveTextContent('2')
    expect(screen.getByRole('navigation', { name: '市场状态带' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '证据健康' })).toBeInTheDocument()
  })

  it('renders the homepage around return, automated process, and chart without decision wording', () => {
    render(<App />)

    expect(screen.getByLabelText('收益结果')).toBeInTheDocument()
    expect(screen.getByLabelText('自动化过程')).toBeInTheDocument()
    expect(screen.getAllByText('收益曲线').length).toBeGreaterThan(0)
    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    expect(within(marketHeader).getByText('运行中').parentElement).toHaveTextContent('1')
    expect(within(marketHeader).getByText('已完成').parentElement).toHaveTextContent('2')
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'true')
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '实盘' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('模拟执行').length).toBeGreaterThan(0)
    expect(screen.queryByRole('tablist', { name: '收益区间' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '机会从全市场进入，只把可执行结果留在首页。' })).not.toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '当前运行' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '本轮结果' })).not.toBeInTheDocument()
    expect(screen.queryByText('现在判断')).not.toBeInTheDocument()
    expect(screen.queryByText('看决策')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '总览' })).toBeInTheDocument()
  })

  it('renders one continuous workbench with chart, review rail, and blotter', () => {
    render(<App />)

    const workbench = screen.getByRole('region', { name: '交易工作台' })
    expect(within(workbench).getByRole('region', { name: '收益与目标' })).toBeInTheDocument()
    expect(within(workbench).getByRole('complementary', { name: '当前运行' })).toBeInTheDocument()
    expect(within(workbench).getByRole('tablist', { name: '工作台明细' })).toBeInTheDocument()
    expect(screen.getAllByRole('region', { name: '交易工作台' })).toHaveLength(1)

    const chart = within(workbench).getByRole('img', { name: '模拟盘收益曲线' })
    expect(chart).toHaveAttribute('aria-describedby')
    expect(within(chart).queryByRole('button')).not.toBeInTheDocument()
  })

  it('gates live mode without exposing execution controls', () => {
    render(<App />)

    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    click(screen.getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toHaveTextContent('实盘待接入')
    expect(screen.getAllByText('模拟盘参考')).toHaveLength(2)
    expect(within(marketHeader).getByText('当前收益').parentElement).toHaveTextContent('待接入')
    expect(within(marketHeader).getByText('模拟盘参考')).toBeInTheDocument()
    expect(screen.queryByText('market_data_missing')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /买|卖|下单|确认交易/ })).not.toBeInTheDocument()

    click(screen.getByRole('button', { name: '收益' }))
    expect(screen.getByRole('region', { name: '实盘接入状态' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: '模拟盘收益曲线' })).not.toBeInTheDocument()
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

    await waitFor(() => expect(screen.getByRole('complementary', { name: '当前运行' })).toHaveTextContent('0700.HK'))
    click(screen.getByRole('tab', { name: '自动复盘 1' }))
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

    await waitFor(() => expect(screen.getByText('等待收益写入')).toBeInTheDocument())
    expect(screen.getAllByText('当前没有运行中的自动过程').length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: '当前没有运行中的自动任务' })).toBeInTheDocument()
    expect(screen.queryByText(/等待新机会 · 转化 0%/)).not.toBeInTheDocument()
    click(screen.getByRole('tab', { name: '持仓 0' }))
    expect(screen.getByText('暂无持仓记录')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('does not show demo data when production snapshot is unavailable', async () => {
    vi.stubEnv('VITE_TRADING_AGENT_DEMO_PREVIEW', '0')
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('snapshot unavailable')
    }))

    render(<App />)

    await waitFor(() => expect(screen.getByText('等待收益写入')).toBeInTheDocument())
    expect(screen.getByText('等待接口')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
    expect(screen.queryByText('600519.SH')).not.toBeInTheDocument()
    expect(screen.queryByText('+9.42%')).not.toBeInTheDocument()
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
    expect(screen.getAllByText('自动化过程').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓跟踪').length).toBeGreaterThan(0)
    expect(screen.getAllByText('当前没有运行中的自动过程').length).toBeGreaterThan(0)
    expect(screen.getAllByText('1 个持仓继续跟踪').length).toBeGreaterThan(0)
    expect(screen.getAllByText('承压').length).toBeGreaterThan(0)
    expect(screen.getAllByText('运行空闲').length).toBeGreaterThan(0)
    expect(screen.queryByText(/0 个新机会 · 1 个持仓在跟踪 · 转化/)).not.toBeInTheDocument()
    expect(screen.queryByText(/暂无新信号进入/)).not.toBeInTheDocument()
    expect(screen.getAllByText('¥19.99万').length).toBeGreaterThan(0)
    expect(screen.getAllByText('复盘收益').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/可复盘/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('0/13').length).toBeGreaterThan(0)
    expect(screen.queryByText(/可复盘 0\/13 · 链路验证 13/)).not.toBeInTheDocument()
  })

  it('keeps the returns page chart summary aligned with the all-market headline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-11T09:00:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
              signals: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
            },
            performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
            portfolio: {
              pnlAmount: -65,
              returnPct: -0.03,
              capitalBase: 200000,
              targetPct: 8,
              maxDrawdownPct: 0,
              tradeCount: 5,
              pointCount: 1,
              source: 'account',
              pnlCurrency: 'CNY',
              updatedAt: '2026-07-11T09:00:00.000Z',
            },
            marketSummaries: [{
              market: 'A-share',
              status: 'ready',
              runtimeState: 'normal',
              holdingCount: 3,
              signalCount: 0,
              tradeCount: 3,
              styleCount: 1,
              capitalBase: 200000,
              pnlAmount: 6931,
              pnlCurrency: 'CNY',
              returnPct: 3.47,
              maxDrawdownPct: 0,
              source: 'market-summary',
              headline: 'A股',
              detail: 'A股结果',
            }],
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

    await waitFor(() => expect(screen.getAllByText('+3.47%').length).toBeGreaterThan(0))
    click(screen.getByRole('button', { name: '收益' }))

    expect(screen.getByRole('img', { name: '模拟盘收益曲线' })).toBeInTheDocument()
    expect(screen.getByLabelText('收益曲线摘要')).toHaveTextContent('当前收益 +3.47%')
    expect(screen.queryByText('当前收益 -0.03%')).not.toBeInTheDocument()
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
              { id: 'd', symbol: '600519.SH', market: 'A-share', stage: '待确认', status: '等待', label: '待执行', source: 'signal_queue' },
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

    await waitFor(() => expect(screen.getByLabelText('最近管道事件')).toBeInTheDocument())
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('研究').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('模拟执行').length).toBeGreaterThan(0)
    expect(screen.getAllByText('结果写回').length).toBeGreaterThan(0)
    expect(screen.getByText(/1 条安全拦截/)).toBeInTheDocument()
    expect(screen.getByLabelText('最近管道事件')).toHaveTextContent('600519.SH')
  })

  it('switches from the return card into the dedicated live gate and back', () => {
    render(<App />)

    click(screen.getByRole('tab', { name: '已完成 2' }))
    const card = screen.getByLabelText('收益结果')
    click(within(card).getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toHaveTextContent('实盘待接入')
    click(screen.getByRole('button', { name: '返回模拟盘' }))
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '已完成 2' })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows the Process Book beside its automation inspector', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '过程' }))

    expect(screen.getByRole('region', { name: '过程终端' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: '运行中过程账本' })).toBeInTheDocument()
    expect(within(screen.getByLabelText('过程终端检查器')).getByRole('heading', { name: '过程分布' })).toBeInTheDocument()
    expect(screen.getByText('IF2601.CFFEX')).toBeInTheDocument()
    expect(screen.queryByText('BTC-USD')).not.toBeInTheDocument()
  })

  it('keeps the reserved live state inside the workbench rather than a dialog', () => {
    render(<App />)

    const card = screen.getByLabelText('收益结果')
    click(within(card).getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toBeInTheDocument()
    expect(screen.queryByLabelText('收益结果')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '实盘接入状态' })).not.toBeInTheDocument()
  })

  it('links a chart event marker to the related process view', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '查看 5月28日 过程' }))

    expect(screen.getByRole('region', { name: '过程终端' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: '运行中过程账本' })).toBeInTheDocument()
  })

  it('renders compact process distribution and completion metrics', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '过程' }))

    const inspector = screen.getByLabelText('过程终端检查器')
    expect(within(inspector).getByRole('heading', { name: '过程分布' })).toBeInTheDocument()
    expect(within(inspector).getByText('结果写回')).toBeInTheDocument()
    expect(within(inspector).getByText('安全拦截')).toBeInTheDocument()
  })
})
