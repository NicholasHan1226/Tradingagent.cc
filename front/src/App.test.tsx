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
    expect(screen.getByLabelText('机会漏斗')).toBeInTheDocument()
    expect(screen.getAllByText('收益曲线').length).toBeGreaterThan(0)
    expect(within(screen.getByLabelText('实时收益')).getByRole('button', { name: '模拟盘' })).toBeInTheDocument()
    expect(within(screen.getByLabelText('实时收益')).getByRole('button', { name: '实盘' })).toBeInTheDocument()
    expect(screen.getByText('发现')).toBeInTheDocument()
    expect(screen.getByText('风控')).toBeInTheDocument()
    expect(screen.getAllByText('交易信号').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: '机会从全市场进入，只把可执行结果留在首页。' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '现在关注' })).toBeInTheDocument()
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
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText('2 个机会进入，2 个形成交易信号 · 留存 100%')).toBeInTheDocument())
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
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText('等待收益数据')).toBeInTheDocument())
    expect(screen.getAllByText('暂无新机会').length).toBeGreaterThan(0)
    expect(screen.getByText('暂无持仓记录')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('switches the return card between simulated and reserved live mode in place', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    fireEvent.click(within(card).getByRole('button', { name: '实盘' }))

    expect(within(card).getAllByText('实盘未启用').length).toBeGreaterThan(0)
    expect(within(card).getByText('授权和风控开关完成后，这里切换到真实账户结果。')).toBeInTheDocument()
  })

  it('shows actionable opportunity summary before the opportunity table', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '机会' }))

    expect(screen.getByRole('heading', { name: '当前可处理机会' })).toBeInTheDocument()
    expect(screen.getByText('可处理机会')).toBeInTheDocument()
    expect(screen.getByText('预期机会')).toBeInTheDocument()
    expect(screen.getByText('BTC-USD')).toBeInTheDocument()
    expect(screen.getByText('0700.HK')).toBeInTheDocument()
  })

  it('keeps the reserved live state inside the return card', () => {
    render(<App />)

    const card = screen.getByLabelText('实时收益')
    fireEvent.click(within(card).getByRole('button', { name: '实盘' }))

    expect(within(card).getAllByText('实盘未启用').length).toBeGreaterThan(0)
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
    expect(screen.getByText('流失 34.3%')).toBeInTheDocument()
    expect(screen.getByText('已兑现 59.3%')).toBeInTheDocument()
  })
})
