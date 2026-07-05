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
      <div className="terminal-table opportunity-table">
        <div className="terminal-row terminal-head">
          <span>机会</span>
          <span>市场</span>
          <span>当前结果</span>
          <span>还差什么</span>
          <span>有效期</span>
          <span>预期影响</span>
          <span>风险</span>
        </div>
        {signals.map((signal) => (
          <div className="terminal-row" key={signal.symbol}>
            <AssetCell symbol={signal.symbol} name={signal.name} />
            <span>{marketLabels[signal.market]}</span>
            <span>{signal.reason}</span>
            <span>{signal.next}</span>
            <span>{signal.age}</span>
            <span className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'}>{signal.impact}</span>
            <span>{signal.status === 'blocked' ? '偏高' : '可观察'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
