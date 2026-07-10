import { marketLabels } from '../../data/dashboard'
import type { SignalRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'

export function OpportunityTable({ signals }: { signals: SignalRow[] }) {
  const blockedCount = signals.filter((signal) => signal.status === 'blocked').length
  const pendingCount = signals.filter((signal) => signal.status === 'pending').length
  const impactTotal = signals.reduce((total, signal) => {
    const value = Number(signal.impact.replace('+', ''))
    return Number.isFinite(value) && value > 0 ? total + value : total
  }, 0)

  return (
    <div className="opportunity-stack">
      <div className="opportunity-summary" aria-label="机会汇总">
        <span>
          <em>可处理机会</em>
          <strong>{signals.length}</strong>
        </span>
        <span>
          <em>预期机会</em>
          <strong>+{impactTotal.toFixed(1)}</strong>
        </span>
        <span>
          <em>等待确认</em>
          <strong>{pendingCount}</strong>
        </span>
        <span>
          <em>风险拦截</em>
          <strong>{blockedCount}</strong>
        </span>
      </div>
      <div aria-label="当前机会表" className="terminal-table opportunity-table" role="table">
        <div className="terminal-row terminal-head" role="row">
          <span role="columnheader">机会</span>
          <span role="columnheader">市场</span>
          <span role="columnheader">当前结果</span>
          <span role="columnheader">还差什么</span>
          <span role="columnheader">有效期</span>
          <span role="columnheader">预期影响</span>
          <span role="columnheader">风险</span>
        </div>
        {signals.map((signal, index) => (
          <div className="terminal-row" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`} role="row">
            <div role="cell"><AssetCell symbol={signal.symbol} name={signal.name} /></div>
            <span role="cell">{marketLabels[signal.market]}</span>
            <span role="cell">{signal.reason}</span>
            <span role="cell">{signal.next}</span>
            <span role="cell">{signal.age}</span>
            <span className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'} role="cell">{signal.impact}</span>
            <span role="cell">{signal.status === 'blocked' ? '偏高' : '可观察'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
