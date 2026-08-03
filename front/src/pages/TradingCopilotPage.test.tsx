import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { analysisFromSignal } from '../copilot/analysis'
import { TradingCopilotPage } from './TradingCopilotPage'

beforeEach(() => {
  window.localStorage.clear()
  Object.defineProperty(window, 'scrollTo', { configurable: true, value: vi.fn(), writable: true })
})
afterEach(() => vi.unstubAllGlobals())

describe('TradingCopilotPage', () => {
  it('turns a sourced A-share snapshot signal into a bounded observation rather than an order', () => {
    const analysis = analysisFromSignal({
      symbol: '600519.SH', name: '贵州茅台', market: 'A-share', method: '冻结排名', status: 'pending',
      impact: '--', confidence: '86%', age: '2m', reason: '价格结构进入观察区', next: '等待量价确认', steps: 3,
    }, '2026-08-02T00:00:00.000Z')
    expect(analysis).toMatchObject({ mode: 'tradingagent_observation', verdict: '等待条件', evidenceStrength: { value: null, semantics: 'unavailable' }, readiness: { evidence: 'unscored_observation', action: 'observe_only' } })
    expect(analysis.oppose[0]?.detail).toContain('不会把单向信号当成确定买入结论')
  })

  it('shows the demo boundary, edits capital, and records human intent without order language', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)

    expect(await screen.findByText('等待现役清单投影')).toBeInTheDocument()
    expect(document.querySelector('.copilot-sidebar')).toBeNull()
    expect(screen.getAllByText('演示分析').length).toBeGreaterThan(0)
    expect(screen.getByText('仅人工研究，不连接券商')).toBeInTheDocument()
    expect(screen.getByText('公司资料')).toBeInTheDocument()
    expect(screen.getByText('Copilot 证据共识')).toBeInTheDocument()
    expect(screen.getByText('舆论与事件温度')).toBeInTheDocument()
    expect(screen.getByText('偏积极')).toBeInTheDocument()
    expect(screen.getByText('等待正式覆盖')).toBeInTheDocument()
    expect(screen.getByText('正式研究条件未覆盖')).toBeInTheDocument()
    expect(screen.getByText('正式失效条件未覆盖，不能进入人工计划。')).toBeInTheDocument()
    expect(screen.getByLabelText('数据集活跃状态')).toHaveTextContent('demo_fixture · 时钟覆盖缺口')
    expect(screen.queryByText('收盘站上人工观察位并有量能确认')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /调整资金/ }))
    const capital = screen.getByLabelText('申报总资金（元）')
    fireEvent.change(capital, { target: { value: '300000' } })
    fireEvent.click(screen.getByRole('button', { name: '保存资金信息' }))
    expect(await screen.findByText('¥300,000')).toBeInTheDocument()

    expect(screen.getByRole('button', { name: '加入人工计划' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '继续观察' }))
    fireEvent.change(screen.getByLabelText('决策理由'), { target: { value: '等待量价确认' } })
    fireEvent.click(screen.getByRole('button', { name: '写入人工决策账本' }))
    expect(await screen.findByText(/继续观察（未下单）/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/演示修改未保存/)).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: '决策记录' }))
    expect(screen.getByText('等待量价确认')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '记录复盘' }))
    fireEvent.change(screen.getByLabelText('状态'), { target: { value: 'not_executed' } })
    fireEvent.change(screen.getByLabelText('实际动作'), { target: { value: '未交易' } })
    fireEvent.change(screen.getByLabelText('复盘备注'), { target: { value: '正式数据门禁未满足' } })
    fireEvent.click(screen.getByRole('button', { name: '保存复盘' }))
    expect(await screen.findByText('复盘：正式数据门禁未满足')).toBeInTheDocument()
    expect(window.localStorage.length).toBe(0)
  })

  it('adds an uncovered A-share and fails closed to no formal analysis', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('等待现役清单投影')
    fireEvent.click(screen.getByRole('button', { name: '关注列表' }))
    fireEvent.change(screen.getByLabelText('输入股票代码和名称'), { target: { value: '000001.SZ 平安银行' } })
    fireEvent.click(screen.getByRole('button', { name: '加入关注' }))
    fireEvent.click(screen.getByRole('button', { name: /平安银行.*000001\.SZ/ }))
    expect((await screen.findAllByText('暂无正式分析')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '加入人工计划' })).toBeDisabled()
    expect(screen.getByText('行情图表暂不可用')).toBeInTheDocument()
    expect(screen.getByText('当前没有按股票代码验证通过的公告、新闻或舆情数据。')).toBeInTheDocument()
    expect(screen.getByText('暂无舆论')).toBeInTheDocument()
    expect(screen.getByText('研究建议与条件')).toBeInTheDocument()
    expect(screen.getByText('等待正式覆盖')).toBeInTheDocument()
    expect(screen.getByText(/当前不生成买入或卖出建议/)).toBeInTheDocument()
    expect(screen.getByText('事件覆盖未到位')).toBeInTheDocument()
  })

  it('switches chart ranges, forecast views, and stock-linked event content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('等待现役清单投影')

    expect(screen.getByRole('img', { name: '许继电气 1D 行情图' })).toBeInTheDocument()
    expect(screen.getAllByText('演示公告：项目进展提示').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('tab', { name: '1M' }))
    expect(screen.getByRole('img', { name: '许继电气 1M 行情图' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '该周期无预测' })).toBeDisabled()
    fireEvent.click(screen.getByRole('tab', { name: '1D' }))

    const forecastToggle = screen.getByRole('button', { name: '显示预测' })
    expect(screen.getAllByText('研究演示 · 概率停显').length).toBeGreaterThan(0)
    fireEvent.click(forecastToggle)
    expect(screen.getByRole('button', { name: '预测已显示' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('img', { name: '许继电气 1D 行情与研究预测图' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: '预测' }))
    expect(screen.getByText('方向研究情景')).toBeInTheDocument()
    expect(screen.getByText('预测交付门禁')).toBeInTheDocument()
    expect(screen.getAllByText('研究演示 · 概率停显').length).toBeGreaterThan(1)
    expect(screen.queryByText(/50%|80%|向上 \d+%|向下 \d+%/)).not.toBeInTheDocument()
    expect(screen.getByText('Challenger · 同门禁对照')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '关注列表' }))
    fireEvent.click(screen.getByRole('button', { name: /比亚迪.*002594\.SZ/ }))
    fireEvent.click(screen.getByRole('tab', { name: '概述' }))
    expect((await screen.findAllByText('演示公告：月度经营数据说明')).length).toBeGreaterThan(0)
    expect(screen.queryByText('演示公告：项目进展提示')).not.toBeInTheDocument()
  })

  it('separates the complete portfolio from the selected stock relationship', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('等待现役清单投影')

    expect(screen.getByText('当前个股持仓')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /查看全部 2 只持仓/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '资金与持仓' }))
    expect(screen.getByRole('heading', { name: '资金与持仓' })).toBeInTheDocument()
    expect(screen.getAllByText('2 只申报持仓').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: /许继电气.*000400\.SZ/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /紫金矿业.*601899\.SH/ })).toBeInTheDocument()
  })

  it('distinguishes holdings from watch-only stocks in the watchlist', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('等待现役清单投影')
    fireEvent.click(screen.getByRole('button', { name: '关注列表' }))

    expect(screen.getByText('4 只关注 · 2 只持仓')).toBeInTheDocument()
    expect(screen.getAllByText('持仓')).toHaveLength(2)
    expect(screen.getAllByText('仅关注')).toHaveLength(2)
  })

  it('shows the screenshot-aligned research preview without injecting demo account state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled={false} onOpenQuant={() => undefined} />)

    expect(await screen.findByText('研究界面预览 · 个人状态仍为空')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '许继电气' })).toBeInTheDocument()
    expect(screen.getByText('当前是完整界面演示：不属于你的关注、持仓或决策记录')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '许继电气 1D 行情图' })).toBeInTheDocument()
    expect(screen.getAllByText('¥0').length).toBeGreaterThanOrEqual(3)
    expect(screen.getByLabelText('用户申报账户摘要')).toHaveTextContent('关注股票0 只')
    expect(screen.getByRole('button', { name: '加入人工计划' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '关注列表' }))
    expect(screen.getByText('还没有关注股票')).toBeInTheDocument()
    expect(screen.queryByText('紫金矿业')).not.toBeInTheDocument()
  })

  it('opens an arbitrary A-share terminal before adding it to the personal watchlist', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled={false} onOpenQuant={() => undefined} />)
    await screen.findByText('研究界面预览 · 个人状态仍为空')

    fireEvent.change(screen.getByLabelText('搜索 A 股并打开个股终端'), { target: { value: '000001.SZ 平安银行' } })
    fireEvent.click(screen.getByRole('button', { name: '打开个股' }))
    expect(await screen.findByRole('heading', { name: '平安银行' })).toBeInTheDocument()
    expect(screen.getAllByText('暂无正式分析').length).toBeGreaterThan(0)
    expect(screen.getByText('行情图表暂不可用')).toBeInTheDocument()
    expect(screen.getByLabelText('用户申报账户摘要')).toHaveTextContent('关注股票0 只')

    fireEvent.click(screen.getByRole('button', { name: '加入关注' }))
    await waitFor(() => expect(screen.getByLabelText('用户申报账户摘要')).toHaveTextContent('关注股票1 只'))
    expect(screen.getByRole('button', { name: '已关注' })).toBeDisabled()
  })
})
