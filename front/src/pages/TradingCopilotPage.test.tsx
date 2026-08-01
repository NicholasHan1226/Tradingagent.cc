import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { analysisFromSignal } from '../copilot/analysis'
import { TradingCopilotPage } from './TradingCopilotPage'

afterEach(() => vi.unstubAllGlobals())

describe('TradingCopilotPage', () => {
  it('turns a sourced A-share snapshot signal into a bounded observation rather than an order', () => {
    const analysis = analysisFromSignal({
      symbol: '600519.SH', name: '贵州茅台', market: 'A-share', method: '冻结排名', status: 'pending',
      impact: '--', confidence: '86%', age: '2m', reason: '价格结构进入观察区', next: '等待量价确认', steps: 3,
    }, '2026-08-02T00:00:00.000Z')
    expect(analysis).toMatchObject({ mode: 'tradingagent_observation', score: 86, verdict: '等待条件' })
    expect(analysis.oppose[0]?.detail).toContain('不会把单向信号当成确定买入结论')
  })

  it('shows the demo boundary, edits capital, and records human intent without order language', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)

    expect(await screen.findByText('今天先看条件，再做决定')).toBeInTheDocument()
    expect(screen.getAllByText('演示分析').length).toBeGreaterThan(0)
    expect(screen.getByText('Copilot 只记录计划，不连接券商、不自动下单。')).toBeInTheDocument()
    expect(screen.getByText('公司资料')).toBeInTheDocument()
    expect(screen.getByText('Copilot 证据共识')).toBeInTheDocument()
    expect(screen.getByText('舆论与事件温度')).toBeInTheDocument()
    expect(screen.getByText('偏积极')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /调整资金/ }))
    const capital = screen.getByLabelText('申报总资金（元）')
    fireEvent.change(capital, { target: { value: '300000' } })
    fireEvent.click(screen.getByRole('button', { name: '保存资金信息' }))
    expect(await screen.findByText('¥300,000')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '加入人工计划' }))
    expect(await screen.findByText(/加入计划（未下单）/)).toBeInTheDocument()
    await waitFor(() => expect(window.localStorage.length).toBe(1))
  })

  it('adds an uncovered A-share and fails closed to no formal analysis', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('今天先看条件，再做决定')
    fireEvent.change(screen.getByLabelText('输入股票代码和名称'), { target: { value: '000001.SZ 平安银行' } })
    fireEvent.click(screen.getByRole('button', { name: '加入关注' }))
    expect((await screen.findAllByText('暂无正式分析')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '加入人工计划' })).toBeDisabled()
    expect(screen.getByText('行情图表暂不可用')).toBeInTheDocument()
    expect(screen.getByText('当前没有按股票代码验证通过的公告、新闻或舆情数据。')).toBeInTheDocument()
    expect(screen.getByText('暂无舆论')).toBeInTheDocument()
  })

  it('switches chart ranges, forecast views, and stock-linked event content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<TradingCopilotPage demoPreviewEnabled onOpenQuant={() => undefined} />)
    await screen.findByText('今天先看条件，再做决定')

    expect(screen.getByRole('img', { name: '许继电气 1D 行情与预测图' })).toBeInTheDocument()
    expect(screen.getAllByText('演示公告：项目进展提示').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('tab', { name: '1M' }))
    expect(screen.getByRole('img', { name: '许继电气 1M 行情图' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '该周期无预测' })).toBeDisabled()
    fireEvent.click(screen.getByRole('tab', { name: '1D' }))

    const forecastToggle = screen.getByRole('button', { name: '显示预测' })
    fireEvent.click(forecastToggle)
    expect(screen.getByRole('button', { name: '预测已显示' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('tab', { name: '预测' }))
    expect(screen.getByText('方向研究情景')).toBeInTheDocument()
    expect(screen.getByText('预测交付门禁')).toBeInTheDocument()
    expect(screen.getByText('研究演示 · 概率停显')).toBeInTheDocument()
    expect(screen.queryByText(/50%|80%|向上 \d+%|向下 \d+%/)).not.toBeInTheDocument()
    expect(screen.getByText('Challenger · 同门禁对照')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /比亚迪.*002594\.SZ.*61.*等待/ }))
    fireEvent.click(screen.getByRole('tab', { name: '概述' }))
    expect((await screen.findAllByText('演示公告：月度经营数据说明')).length).toBeGreaterThan(0)
    expect(screen.queryByText('演示公告：项目进展提示')).not.toBeInTheDocument()
  })
})
