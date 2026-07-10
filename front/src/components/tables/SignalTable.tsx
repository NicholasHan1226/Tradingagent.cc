import { marketLabels, statusLabels } from '../../data/dashboard'
import type { SignalRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'
import { Timeline } from '../Timeline'

const stages = ['发现', '成形', '复核', '风控', '推进', '结果']

export function SignalTable({ signals }: { signals: SignalRow[] }) {
  return (
    <div aria-label="结果与复盘表" className="terminal-table signal-table" role="table">
      <div className="terminal-row terminal-head" role="row">
        <span role="columnheader">标的</span>
        <span role="columnheader">市场</span>
        <span role="columnheader">结果</span>
        <span role="columnheader">策略</span>
        <span role="columnheader">为什么</span>
        <span role="columnheader">过程</span>
        <span role="columnheader">影响</span>
        <span role="columnheader">下次规则</span>
        <span role="columnheader">时间</span>
      </div>
      {signals.map((signal, index) => (
        <div className="terminal-row" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`} role="row">
          <div role="cell"><AssetCell symbol={signal.symbol} name={signal.name} /></div>
          <span role="cell">{marketLabels[signal.market]}</span>
          <span className={`status ${signal.status}`} role="cell">{signal.queueBucket?.toLowerCase() === 'partial' ? '部分成交' : statusLabels[signal.status]}</span>
          <span role="cell" title={signal.signalSource ? `来源：${signal.signalSource}` : undefined}>{signal.strategyName ?? signal.method}</span>
          <span role="cell">{signal.reason}</span>
          <div role="cell"><Timeline steps={signal.steps} labels={stages} /></div>
          <span className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'} role="cell">{signal.impact}</span>
          <span role="cell">{signal.next}</span>
          <span role="cell">{signal.age}</span>
        </div>
      ))}
    </div>
  )
}
