import type { ProcessEventRow } from '../../lib/processEventViewModel'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'

export function ProcessEventLedger({ rows }: { rows: ProcessEventRow[] }) {
  return (
    <section className="terminal-table-panel process-event-ledger">
      <TerminalPanelHeader eyebrow="EVENT STREAM" meta={`${rows.length} 条`} title="过程事件" />
      {rows.length ? (
        <div className="terminal-table-scroll">
          <table aria-label="过程事件账本" className="terminal-table">
            <thead><tr><th>时间</th><th>资产</th><th>市场</th><th>阶段</th><th>事件</th><th>来源</th><th>延迟</th><th>原因</th></tr></thead>
            <tbody>{rows.map((row) => (
              <tr key={row.id}>
                <td>{row.timestamp}</td><td><strong>{row.symbol}</strong></td><td>{row.market}</td><td>{row.stage}</td>
                <td><span className={`terminal-state ${eventTone(row.result)}`}>{row.result}</span></td>
                <td>{row.source}</td><td>{row.latency}</td><td className="reason-cell" title={row.reason}>{row.reason}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <TerminalEmpty title="暂无过程事件" detail="形成真实事件后，将按时间与序列展示只读审计轨迹。" />}
    </section>
  )
}

function eventTone(result: string) {
  if (/成交|写回|通过/.test(result)) return 'positive'
  if (/拦截|拒绝|取消|放弃/.test(result)) return 'negative'
  if (/等待|复盘/.test(result)) return 'warning'
  return ''
}
