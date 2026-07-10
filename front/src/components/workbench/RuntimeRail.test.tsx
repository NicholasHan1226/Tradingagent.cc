import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RuntimeRail } from './RuntimeRail'
import type { AutomationRuntimeItem } from '../../lib/automationObservatoryViewModel'

const running: AutomationRuntimeItem = {
  kind: 'running',
  contextLabel: '当前运行',
  symbol: 'IF2601.CFFEX',
  name: '沪深300期指',
  market: 'CNFutures',
  strategy: '期货自动观察',
  stage: '模拟执行',
  statusLabel: '自动运行中',
  evidenceLabel: '证据有限',
  updatedAtLabel: '31分钟',
  detail: '等待 5 分钟样本和模拟回执',
}

describe('RuntimeRail', () => {
  it('shows current automation without a manual action', () => {
    render(<RuntimeRail item={running} runningCount={2} />)

    const rail = screen.getByRole('complementary', { name: '当前运行' })
    expect(rail).toHaveTextContent('运行中 2')
    expect(rail).toHaveTextContent('模拟执行')
    expect(rail).toHaveTextContent('证据有限')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(rail).not.toHaveTextContent(/下一步|还差什么|查看完整记录|需要复盘/)
  })

  it('shows an idle system without inventing a task', () => {
    render(<RuntimeRail item={{
      ...running,
      kind: 'idle',
      contextLabel: '运行空闲',
      symbol: null,
      market: null,
      name: '当前没有运行中的自动任务',
      stage: '运行空闲',
      statusLabel: '等待下一轮调度',
    }} runningCount={0} />)

    expect(screen.getByRole('complementary', { name: '运行空闲' })).toHaveTextContent('等待下一轮调度')
  })
})
