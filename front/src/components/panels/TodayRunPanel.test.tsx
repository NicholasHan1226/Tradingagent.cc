import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PaperDayRunSummary } from '../../types/dashboard'
import { TodayRunPanel } from './TodayRunPanel'

const run: PaperDayRunSummary = {
  environment: 'local_candidate',
  productionVerified: false,
  contractId: 'tradingagent.paper_day_loop.v1',
  runId: 'ashare-paper-day-fixture',
  tradeDate: '2026-07-16',
  status: 'completed',
  currentStage: 'reported',
  completedStageCount: 9,
  totalStageCount: 9,
  dataEvidenceState: 'ready',
  simulationExecutionState: 'eligible',
  candidateCount: 2,
  decisionCount: 1,
  simulatedOrderCount: 1,
  simulatedFillCount: 1,
  noTradeReasons: [],
  riskBlocks: [],
  championManifestSha256: 'c'.repeat(64),
  llmEvidenceState: 'evidence_only',
  source: 'shared/runtime/run_bundles/latest.json',
}

describe('TodayRunPanel', () => {
  it('shows the local non-production paper-day result without action controls', () => {
    render(<TodayRunPanel run={run} />)

    const panel = screen.getByRole('region', { name: '今日自动模拟盘状态' })
    expect(panel).toHaveTextContent('本地候选 · 非生产')
    expect(panel).toHaveTextContent('数据证据可用')
    expect(panel).toHaveTextContent('仅模拟可执行')
    expect(panel).toHaveTextContent('候选 2')
    expect(panel).toHaveTextContent('决策 1')
    expect(panel).toHaveTextContent('模拟成交 1')
    expect(panel).toHaveTextContent('Champion 清单已声明')
    expect(panel).toHaveTextContent('LLM 仅作证据')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows honest unavailable state when no published RunBundle exists', () => {
    render(<TodayRunPanel />)

    const panel = screen.getByRole('region', { name: '今日自动模拟盘状态' })
    expect(panel).toHaveTextContent('今日 RunBundle 不可用')
    expect(panel).toHaveTextContent('未发现已发布的本地候选快照')
    expect(panel).not.toHaveTextContent('运行正常')
    expect(panel).not.toHaveTextContent('实时')
  })

  it('prioritizes risk and no-trade reasons over a false success label', () => {
    render(<TodayRunPanel run={{
      ...run,
      status: 'completed_with_blocks',
      simulationExecutionState: 'blocked',
      simulatedOrderCount: 0,
      simulatedFillCount: 0,
      noTradeReasons: ['no_execution_eligible_candidates'],
      riskBlocks: ['dataset_stale'],
    }} />)

    const panel = screen.getByRole('region', { name: '今日自动模拟盘状态' })
    expect(panel).toHaveTextContent('新开仓受阻')
    expect(panel).toHaveTextContent('未交易：无可执行候选')
    expect(panel).toHaveTextContent('风险阻断：数据已滞后')
    expect(panel).not.toHaveTextContent('no_execution_eligible_candidates')
    expect(panel).not.toHaveTextContent('dataset_stale')
    expect(panel).not.toHaveTextContent('仅模拟可执行')
  })
})
