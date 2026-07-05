import { marketLabels, statusLabels } from '../../data/dashboard'
import type { SignalRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'
import { Timeline } from '../Timeline'

const stages = ['发现', '成形', '复核', '风控', '推进', '结果']

export function SignalTable({ signals }: { signals: SignalRow[] }) {
  return (
    <div className="terminal-table signal-table">
      <div className="terminal-row terminal-head">
        <span>标的</span>
        <span>市场</span>
        <span>结果</span>
        <span>为什么</span>
        <span>过程</span>
        <span>影响</span>
        <span>下次规则</span>
        <span>时间</span>
      </div>
      {signals.map((signal, index) => (
        <div className="terminal-row" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`}>
          <AssetCell symbol={signal.symbol} name={signal.name} />
          <span>{marketLabels[signal.market]}</span>
          <span className={`status ${signal.status}`}>{statusLabels[signal.status]}</span>
          <span>{signal.reason}</span>
          <Timeline steps={signal.steps} labels={stages} />
          <span className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'}>{signal.impact}</span>
          <span>{signal.next}</span>
          <span>{signal.age}</span>
        </div>
      ))}
    </div>
  )
}
