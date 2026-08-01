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
  })
})
