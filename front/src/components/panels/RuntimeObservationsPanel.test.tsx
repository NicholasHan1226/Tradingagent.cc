import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { runtimeObservationFixture } from '../../test/runtimeObservationFixture'
import { runtimeObservationState } from '../../types/runtimeObservations'
import { RuntimeObservationsPanel } from './RuntimeObservationsPanel'

describe('independent simulation observations', () => {
  it('shows missing and empty results honestly without demo amounts', () => {
    const { rerender } = render(<RuntimeObservationsPanel activeMarket="All Markets" />)
    expect(screen.getByRole('region')).toHaveTextContent('运行观测未配置或未提供')
    expect(screen.getByRole('region')).not.toHaveTextContent('USDT')
    rerender(<RuntimeObservationsPanel activeMarket="All Markets" observations={{ ...runtimeObservationFixture(), entries: [] }} />)
    expect(screen.getByRole('region')).toHaveTextContent('暂无运行记录')
  })

  it('filters to A-share coverage, retains the old date and shows account disconnection', () => {
    render(<RuntimeObservationsPanel activeMarket="A-share" observations={runtimeObservationFixture()} />)
    const panel = screen.getByRole('region')
    expect(panel).toHaveTextContent('账户未接入')
    expect(panel).toHaveTextContent('数据滞后')
    expect(panel).toHaveTextContent('2026/08/28 09:45:00')
    expect(panel).toHaveTextContent('核验时间 2026/08/30 20:00:00')
    expect(panel).toHaveTextContent('数据时间为覆盖来源的观测时间')
    expect(panel).toHaveTextContent('覆盖30')
    expect(panel).toHaveTextContent('缺失70')
    expect(panel).not.toHaveTextContent('Crypto')
    expect(panel).not.toHaveTextContent('USDT')
    expect(panel.querySelector('time')).toHaveAttribute('dateTime', '2026-08-28T01:45:00Z')
  })

  it('shows only Crypto native ledger amounts with exact decimals, never headline PnL', () => {
    render(<RuntimeObservationsPanel activeMarket="Crypto" observations={runtimeObservationFixture()} />)
    const panel = screen.getByRole('region')
    expect(panel).toHaveTextContent('9234.567890123456789 USDT')
    expect(panel).toHaveTextContent('9981.23 USDT')
    expect(panel).toHaveTextContent('18.77 USDT')
    expect(panel).toHaveTextContent('模拟仓位2')
    expect(panel).toHaveTextContent('模拟订单回执7')
    expect(panel).toHaveTextContent('完成观测轮次12')
    expect(panel).toHaveTextContent('数据拒收3')
    expect(panel).toHaveTextContent('数据时间为市场时段，不代表账本权益截止时刻')
    expect(panel).toHaveTextContent('核验时间 2026/08/30 20:00:00')
    expect(panel).not.toHaveTextContent('分钟覆盖')
    expect(panel).not.toHaveTextContent('-2.34')
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument()
  })

  it('keeps All Markets count-only and does not leak into unsupported markets', () => {
    const { rerender } = render(<RuntimeObservationsPanel activeMarket="All Markets" observations={runtimeObservationFixture()} />)
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(screen.getByRole('region')).toHaveTextContent('模拟订单回执7')
    expect(screen.getByRole('region')).not.toHaveTextContent(/USDT|CNY|9234|9981|18\.77/)
    rerender(<RuntimeObservationsPanel activeMarket="CNFutures" observations={runtimeObservationFixture()} />)
    expect(screen.queryAllByRole('article')).toHaveLength(0)
    expect(screen.getByRole('region')).toHaveTextContent('所选市场暂无独立模拟运行记录')
  })

  it('distinguishes background refresh, failure and invalid source without keeping old metrics', () => {
    const pending = runtimeObservationState('pending', 'runtime_refresh_pending', '2026-08-30T12:00:00Z')
    const { rerender } = render(<RuntimeObservationsPanel activeMarket="Crypto" observations={pending} />)
    expect(screen.getByRole('region')).toHaveTextContent('运行中 / 等待结果')
    expect(screen.getByRole('region')).toHaveTextContent('数据时间 未提供')
    expect(screen.getByRole('region')).toHaveTextContent('刷新发起时间')
    expect(screen.getByRole('region')).not.toHaveTextContent('核验时间')
    rerender(<RuntimeObservationsPanel activeMarket="Crypto" observations={runtimeObservationState('unavailable', 'runtime_reader_timeout', pending.generatedAt)} />)
    expect(screen.getByRole('region')).toHaveTextContent('后台重验超时')
    const invalid = runtimeObservationFixture()
    invalid.entries[1].status = 'invalid'
    delete invalid.entries[1].simulation
    delete invalid.entries[1].counts
    rerender(<RuntimeObservationsPanel activeMarket="Crypto" observations={invalid} />)
    expect(screen.getByRole('region')).toHaveTextContent('数据损坏 / 校验未通过')
    expect(screen.getByRole('region')).not.toHaveTextContent('9234')
    Object.assign(invalid.entries[1], { canonicalAccountConnected: true })
    rerender(<RuntimeObservationsPanel activeMarket="Crypto" observations={invalid} />)
    expect(screen.getByRole('region')).toHaveTextContent('运行数据损坏或未通过合同校验')
    expect(screen.getByRole('region')).not.toHaveTextContent('9234')
  })

  it.each(['内部账户Token 私密正文 arbitrary-secret', '__proto__', 'constructor', 'toString'])('never echoes arbitrary reason text %s', (reason) => {
    const body = runtimeObservationFixture()
    body.entries[1].reason = reason
    render(<RuntimeObservationsPanel activeMarket="Crypto" observations={body} />)
    expect(screen.getByRole('region')).toHaveTextContent('来源未提供可识别的原因说明')
    expect(screen.getByRole('region')).not.toHaveTextContent(/Token|私密|arbitrary-secret/)
  })

  it.each([
    ['source_missing', '本次查找范围内未找到运行记录'],
    ['source_validation_failed', '来源记录未通过完整校验'],
    ['writer_in_progress', '模拟运行正在写入，等待完整记录'],
    ['independent_research_account_not_connected', '独立研究覆盖；未接入正式模拟账户'],
    ['dated_simulated_ledger_not_live_account', '独立模拟账本；未连接真实账户'],
  ])('maps the agreed reason %s without exposing its raw value', (reason, label) => {
    const body = runtimeObservationFixture()
    body.entries[1].reason = reason
    render(<RuntimeObservationsPanel activeMarket="Crypto" observations={body} />)
    expect(screen.getByRole('region')).toHaveTextContent(label)
    expect(screen.getByRole('region')).not.toHaveTextContent(reason)
  })
})
