import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PortfolioLedgerRow, ProcessBookRow, RiskLedgerRow } from '../../lib/terminalViewModels'
import { PortfolioLedger } from './PortfolioLedger'
import { ProcessBook } from './ProcessBook'
import { ProcessEventLedger } from './ProcessEventLedger'
import { ProcessCycleLedger } from './ProcessCycleLedger'
import { RiskLedger } from './RiskLedger'
import { TerminalPageShell } from './TerminalPageShell'

const processRow: ProcessBookRow = {
  symbol: '600030.SH',
  name: '中信证券',
  market: 'A股',
  process: '趋势观察',
  stage: '结果写回',
  state: '已执行',
  evidence: '证据完整',
  latency: '8分钟',
  result: '结果已写回',
  updatedAt: '12分钟前',
  reason: '模拟成交完成',
}

const holdingRow: PortfolioLedgerRow = {
  symbol: '300759.SZ',
  assetName: '',
  market: 'A股',
  role: '模拟盘持仓',
  marketValue: '¥64,486',
  weight: '42.3%',
  pnl: '+¥6,826',
  contribution: '+82.8%',
  risk: '正常',
  quantity: '100',
  averagePrice: '¥580.20',
  markPrice: '¥644.86',
  costBasis: '¥58,020',
  dayPnl: '+¥280',
  source: '持仓快照',
  updatedAt: '10:30',
}

const riskRow: RiskLedgerRow = {
  symbol: 'BTC-USD',
  market: '加密货币',
  stage: '风控',
  gate: '安全拦截',
  evidence: '证据有限',
  reason: '超过风险边界',
  updatedAt: '4分钟前',
}

describe('terminal components', () => {
  it('renders one continuous terminal shell with metrics and ledger', () => {
    render(
      <TerminalPageShell
        heartbeat={{ state: 'idle', headline: '调度正常 · 当前空闲', detail: '最近事件 10分钟前', runningCount: 0, latestEventLabel: '最近事件 10分钟前', snapshotLabel: '快照 刚刚', tone: 'muted' }}
        inspector={<div>自动化检查器</div>}
        ledger={<div>底部账本</div>}
        metrics={[{ label: '收益', value: '+5.2%', tone: 'positive' }, { label: '风险', value: '正常' }]}
        primary={<div>主数据面</div>}
        title="收益终端"
      />,
    )

    const terminal = screen.getByRole('region', { name: '收益终端' })
    expect(within(terminal).getByText('主数据面')).toBeInTheDocument()
    expect(within(terminal).getByText('自动化检查器')).toBeInTheDocument()
    expect(within(terminal).getByText('底部账本')).toBeInTheDocument()
    expect(within(terminal).getByText('+5.2%')).toHaveClass('positive')
  })

  it('renders the process book contract', () => {
    render(<ProcessBook mode="completed" rows={[processRow]} title="最近完成" />)

    const table = screen.getByRole('table', { name: '最近完成过程账本' })
    expect(within(table).getByRole('columnheader', { name: '证据' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: '耗时' })).toBeInTheDocument()
    expect(within(table).getByText('结果已写回')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索账本' }), { target: { value: 'BTC' } })
    expect(screen.getByText('0 条结果')).toBeInTheDocument()
    fireEvent.click(screen.getByText('列'))
    fireEvent.click(screen.getByRole('checkbox', { name: '证据' }))
    expect(within(table).queryByRole('columnheader', { name: '证据' })).not.toBeInTheDocument()
  })

  it('renders the process event audit trail', () => {
    render(<ProcessEventLedger rows={[{
      id: 'event-1', symbol: '600519.SH', market: 'A股', stage: '风控', result: '通过',
      source: '信号队列', latency: '2分钟', reason: '风险检查通过', timestamp: '11:05',
    }]} />)

    const table = screen.getByRole('table', { name: '过程事件账本' })
    expect(within(table).getByText('风险检查通过')).toBeInTheDocument()
    expect(within(table).getByText('信号队列')).toBeInTheDocument()
  })

  it('renders grouped opportunity cycles with honest missing stages', () => {
    render(<ProcessCycleLedger rows={[{
      id: 'opp-1', symbol: '600519.SH', market: 'A股', result: '结果写回', source: '模拟账本',
      latency: '10分钟', evidence: '3/5 阶段', reason: '—', updatedAt: '07/11 13:10',
      stages: [
        { label: '发现', state: 'complete' }, { label: '研判', state: 'missing' },
        { label: '风控', state: 'complete' }, { label: '待确认', state: 'missing' }, { label: '结果', state: 'current' },
      ],
    }]} />)

    const ledger = screen.getByRole('region', { name: '机会周期账本' })
    expect(within(ledger).getByText('决策因果链')).toBeInTheDocument()
    expect(within(ledger).getByText('3/5 阶段 · 10分钟')).toBeInTheDocument()
    expect(within(ledger).getByRole('list', { name: '600519.SH阶段' }).children[1]).toHaveClass('missing')
  })

  it('renders portfolio currency and suppresses duplicate asset names', () => {
    render(<PortfolioLedger rows={[holdingRow]} />)

    const table = screen.getByRole('table', { name: '持仓账本' })
    expect(within(table).getByText('¥64,486')).toBeInTheDocument()
    expect(within(table).getByText('42.3%')).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: '均价' })).toBeInTheDocument()
    expect(within(table).getByText('持仓快照')).toBeInTheDocument()
    expect(within(table).queryByText('300759.SZ', { selector: 'small' })).not.toBeInTheDocument()
  })

  it('renders reviewable risk events as a ledger', () => {
    render(<RiskLedger rows={[riskRow]} />)

    const table = screen.getByRole('table', { name: '风险事件账本' })
    expect(within(table).getByText('安全拦截')).toBeInTheDocument()
    expect(within(table).getByText('超过风险边界')).toBeInTheDocument()
  })
})
