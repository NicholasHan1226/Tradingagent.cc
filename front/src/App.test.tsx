import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources } from './api/tradingAgentReadModel'
import App from './App'

describe('App navigation and result-first dashboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the homepage around return, funnel, and chart without system wording', () => {
    render(<App />)

    expect(screen.getByLabelText('实时收益')).toBeInTheDocument()
    expect(screen.getByLabelText('交易漏斗')).toBeInTheDocument()
    expect(screen.getAllByText('收益曲线').length).toBeGreaterThan(0)
    expect(within(screen.getByLabelText('实时收益')).getByRole('button', { name: '模拟盘' })).toBeInTheDocument()
    expect(within(screen.getByLabelText('实时收益')).getByRole('button', { name: '实盘' })).toBeInTheDocument()
    expect(screen.getAllByText('机会进入').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('信号').length).toBeGreaterThan(0)
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
    expect(screen.getByText('暂无持仓记录')).toBeInTheDocument()
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
    expect(screen.getAllByText('机会进入').length).toBeGreaterThan(0)
    expect(screen.getAllByText('初筛').length).toBeGreaterThan(0)
    expect(screen.getAllByText('研究').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('进入队列').length).toBeGreaterThan(0)
    expect(screen.getAllByText('成交').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放弃').length).toBeGreaterThan(0)
  })

  it('switches the return card between simulated and reserved live mode in place', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    fireEvent.click(within(card).getByRole('button', { name: '实盘' }))

    expect(within(card).getAllByText('实盘待接入').length).toBeGreaterThan(0)
    expect(within(card).getByText('接入后展示真实账户结果；当前以模拟盘为主。')).toBeInTheDocument()
  })

  it('shows actionable opportunity summary before the opportunity table', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '机会' }))

    expect(screen.getByRole('heading', { name: '当前可处理机会' })).toBeInTheDocument()
    expect(screen.getByText('可处理机会')).toBeInTheDocument()
    expect(screen.getByText('预期机会')).toBeInTheDocument()
    expect(screen.getByText('BTC-USD')).toBeInTheDocument()
    expect(screen.getByText('IF2601.CFFEX')).toBeInTheDocument()
  })

  it('keeps the reserved live state inside the return card', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    fireEvent.click(within(card).getByRole('button', { name: '实盘' }))

    expect(within(card).getAllByText('实盘待接入').length).toBeGreaterThan(0)
    expect(screen.queryByRole('dialog', { name: '实盘接入状态' })).not.toBeInTheDocument()
  })

  it('links a chart event marker to the related decision view', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '查看 5月28日 决策' }))

    expect(screen.getByText('决策影响收益')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '从机会到结果' })).toBeInTheDocument()
  })

  it('renders decision formation as a funnel with drop-off rates', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '决策' }))

    expect(screen.getByText('漏斗留存')).toBeInTheDocument()
    expect(screen.getByText('流失 33.3%')).toBeInTheDocument()
    expect(screen.getByText('33% 已兑现')).toBeInTheDocument()
  })
})
