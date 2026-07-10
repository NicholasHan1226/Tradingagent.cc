import type { RiskLedgerRow } from '../../lib/terminalViewModels'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'

export function RiskLedger({ rows }: { rows: RiskLedgerRow[] }) {
  return (
    <section className="terminal-table-panel risk-ledger">
      <TerminalPanelHeader eyebrow="RISK EVENTS" meta={`${rows.length} 条`} title="风险事件账本" />
      {rows.length ? <div className="terminal-table-scroll">
        <table aria-label="风险事件账本" className="terminal-table">
          <thead><tr><th>资产</th><th>市场</th><th>阶段</th><th>处理</th><th>证据</th><th>原因</th><th>更新</th></tr></thead>
          <tbody>{rows.map((row) => <tr key={`${row.symbol}-${row.updatedAt}`}>
            <td><strong>{row.symbol}</strong></td><td>{row.market}</td><td>{row.stage}</td><td><span className={`terminal-state ${gateTone(row.gate)}`}>{row.gate}</span></td><td>{row.evidence}</td><td className="reason-cell">{row.reason}</td><td>{row.updatedAt}</td>
          </tr>)}</tbody>
        </table>
      </div> : <TerminalEmpty title="没有风险事件" detail="当前数据中没有安全拦截、错过或自动终止记录。" />}
    </section>
  )
}

function gateTone(gate: string) {
  return /滞后|隔离|复盘/.test(gate) ? 'warning' : 'negative'
}
