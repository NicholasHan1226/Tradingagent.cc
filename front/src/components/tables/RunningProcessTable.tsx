import { marketLabels } from '../../data/dashboard'
import type { SignalRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'

export function RunningProcessTable({ signals }: { signals: SignalRow[] }) {
  return (
    <div aria-label="自动运行过程表" className="terminal-table running-process-table" role="table">
      <div className="terminal-row terminal-head" role="row">
        <span role="columnheader">自动过程</span>
        <span role="columnheader">市场</span>
        <span role="columnheader">当前阶段</span>
        <span role="columnheader">运行状态</span>
        <span role="columnheader">证据</span>
        <span role="columnheader">更新时间</span>
      </div>
      {signals.map((signal, index) => (
        <div className="terminal-row" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`} role="row">
          <div role="cell">
            <AssetCell symbol={signal.symbol} name={signal.strategyName ?? signal.method} />
          </div>
          <span role="cell">{marketLabels[signal.market]}</span>
          <span role="cell">{formatStage(signal)}</span>
          <span role="cell">{signal.reason}</span>
          <span role="cell">{formatEvidence(signal.stageEvidence)}</span>
          <span role="cell">{signal.age}</span>
        </div>
      ))}
    </div>
  )
}

function formatStage(signal: SignalRow) {
  if (signal.stage === '评分') return '研究'
  if (signal.stage === '待执行') return '模拟执行'
  if (signal.stage === '成交') return '结果写回'
  return signal.stage ?? '自动等待'
}

function formatEvidence(evidence?: SignalRow['stageEvidence']) {
  if (evidence === 'full') return '证据完整'
  if (evidence === 'replay') return '历史回放'
  if (evidence === 'partial') return '证据有限'
  return '证据待写入'
}
