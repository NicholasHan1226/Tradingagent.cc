import type { ProcessBookRow } from '../../lib/terminalViewModels'
import { TerminalPanelHeader } from './TerminalPageShell'

export function ProcessBook({ mode, rows, title }: { mode: 'running' | 'completed' | 'empty'; rows: ProcessBookRow[]; title: string }) {
  return (
    <section className="terminal-table-panel process-book">
      <TerminalPanelHeader eyebrow={mode === 'running' ? 'LIVE PROCESS' : 'PROCESS HISTORY'} meta={`${rows.length} 条`} title={title} />
      {rows.length ? (
        <div className="terminal-table-scroll">
          <table aria-label={`${title}过程账本`} className="terminal-table">
            <thead><tr><th>流程</th><th>市场</th><th>阶段</th><th>状态</th><th>证据</th><th>耗时</th><th>结果</th><th>更新</th></tr></thead>
            <tbody>{rows.map((row) => (
              <tr key={`${row.symbol}-${row.updatedAt}`} title={row.reason}>
                <td><strong>{row.symbol}</strong>{row.name && <small>{row.name}</small>}<span>{row.process}</span></td>
                <td>{row.market}</td><td>{row.stage}</td><td><Tone text={row.state} /></td><td>{row.evidence}</td><td>{row.latency}</td><td>{row.result}</td><td>{row.updatedAt}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <TerminalEmpty title="运行空闲" detail="等待下一轮自动调度；历史结果会在形成后进入过程账本。" />}
    </section>
  )
}

function Tone({ text }: { text: string }) {
  const tone = /执行|成交|写回/.test(text) ? 'positive' : /拦截|取消/.test(text) ? 'negative' : /复盘|等待|部分/.test(text) ? 'warning' : ''
  return <span className={`terminal-state ${tone}`}>{text}</span>
}

export function TerminalEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="terminal-empty"><strong>{title}</strong><span>{detail}</span></div>
}

