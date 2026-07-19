import type { PaperDayRunSummary } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

const REASON_LABELS: Record<string, string> = {
  account_authority_invalid: '账户权威未通过',
  account_unreconciled: '账户尚未对账',
  dataset_failed: '数据读取失败',
  dataset_stale: '数据已滞后',
  no_execution_eligible_candidates: '无可执行候选',
  non_mainboard_decision_leak: '决策超出主板范围',
  non_mainboard_order_leak: '订单超出主板范围',
  non_mainboard_universe_leak: '股票池超出主板范围',
  position_authority_invalid: '持仓权威未通过',
  unknown_simulated_order: '发现未知模拟订单',
}

export function TodayRunPanel({ run }: { run?: PaperDayRunSummary }) {
  return (
    <section className="panel rail-panel today-run-panel" aria-label="今日自动模拟盘状态">
      <PanelTitle kicker="今日状态" title="自动模拟盘" />
      {!run ? <UnavailableTodayRun /> : <TodayRunSummary run={run} />}
    </section>
  )
}

function UnavailableTodayRun() {
  return (
    <div className="today-run-unavailable">
      <strong>今日 RunBundle 不可用</strong>
      <span>未发现已发布的本地候选快照。</span>
      <small>仅说明当前展示证据缺失，不代表自动模拟盘已运行或生产已接入。</small>
    </div>
  )
}

function TodayRunSummary({ run }: { run: PaperDayRunSummary }) {
  const status = formatRunStatus(run.status)
  const execution = formatExecutionState(run.simulationExecutionState)
  const evidence = run.dataEvidenceState === 'ready'
    ? '数据证据可用'
    : run.dataEvidenceState === 'degraded'
      ? '数据证据降级'
      : '数据证据不可用'

  return (
    <div className="today-run-summary">
      <div className="today-run-boundary">
        <strong>本地候选 · 非生产</strong>
        <span>{run.tradeDate}</span>
      </div>
      <div className={`today-run-state ${run.simulationExecutionState}`}>
        <div>
          <span>{status}</span>
          <strong>{execution}</strong>
        </div>
        <b>{run.currentStage ? `${formatStage(run.currentStage)} · ${run.completedStageCount}/${run.totalStageCount}` : `等待首阶段 · 0/${run.totalStageCount}`}</b>
      </div>
      <div className="today-run-metrics" aria-label="今日模拟盘计数">
        <span><em>候选</em>{' '}<b>{run.candidateCount}</b></span>
        <span><em>决策</em>{' '}<b>{run.decisionCount}</b></span>
        <span><em>模拟订单</em>{' '}<b>{run.simulatedOrderCount}</b></span>
        <span><em>模拟成交</em>{' '}<b>{run.simulatedFillCount}</b></span>
      </div>
      <div className="today-run-evidence">
        <span className={run.dataEvidenceState === 'ready' ? 'ready' : 'blocked'}>{evidence}</span>
        <span title={run.championManifestSha256}>Champion 清单已声明</span>
        <span>{run.llmEvidenceState === 'evidence_only' ? 'LLM 仅作证据' : 'LLM 证据未提供'}</span>
      </div>
      <ReasonLine label="未交易" reasons={run.noTradeReasons} />
      <ReasonLine label="风险阻断" reasons={run.riskBlocks} />
      <footer>只读 · 模拟账户 · 不提供交易操作</footer>
    </div>
  )
}

function ReasonLine({ label, reasons }: { label: string; reasons: string[] }) {
  if (!reasons.length) return null
  return <p className="today-run-reasons"><strong>{label}：</strong>{reasons.map(formatReason).join('、')}</p>
}

function formatReason(reason: string) {
  return REASON_LABELS[reason] ?? '未分类原因'
}

function formatRunStatus(status: PaperDayRunSummary['status']) {
  if (status === 'completed') return '本轮记录完整'
  if (status === 'completed_with_blocks') return '完成但存在阻断'
  if (status === 'incomplete_with_blocks') return '运行中且受阻'
  return '本轮尚未完成'
}

function formatExecutionState(state: PaperDayRunSummary['simulationExecutionState']) {
  if (state === 'eligible') return '仅模拟可执行'
  if (state === 'blocked') return '新开仓受阻'
  return '本轮无模拟订单'
}

function formatStage(stage: string) {
  const labels: Record<string, string> = {
    preopen: '盘前检查',
    evidence_ready: '数据证据',
    universe_ready: '股票池',
    decision_ready: '决策',
    risk_checked: '风控',
    orders_simulated: '模拟执行',
    reconciled: '对账',
    learning_recorded: '学习记录',
    reported: '结果写回',
  }
  return labels[stage] ?? '未知阶段'
}
