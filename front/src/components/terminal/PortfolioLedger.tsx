import type { PortfolioLedgerRow } from '../../lib/terminalViewModels'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'

export function PortfolioLedger({ rows }: { rows: PortfolioLedgerRow[] }) {
  return (
    <section className="terminal-table-panel portfolio-ledger">
      <TerminalPanelHeader eyebrow="PORTFOLIO LEDGER" meta={`${rows.length} 项`} title="持仓账本" />
      {rows.length ? <div className="terminal-table-scroll">
        <table aria-label="持仓账本" className="terminal-table">
          <thead><tr><th>资产</th><th>市场</th><th>数量</th><th>均价</th><th>现价</th><th>成本</th><th>市值</th><th>权重</th><th>当日盈亏</th><th>累计盈亏</th><th>贡献</th><th>风险</th><th>来源</th></tr></thead>
          <tbody>{rows.map((row) => <tr key={row.symbol}>
            <td><strong>{row.symbol}</strong>{row.assetName && <small>{row.assetName}</small>}</td>
            <td>{row.market}</td><td>{row.quantity}</td><td>{row.averagePrice}</td><td>{row.markPrice}</td><td>{row.costBasis}</td><td>{row.marketValue}</td>
            <td><div className="exposure-cell"><span style={{ width: row.weight }}>{row.weight}</span></div></td>
            <td className={toneForValue(row.dayPnl)}>{row.dayPnl}</td><td className={toneForValue(row.pnl)}>{row.pnl}</td><td className={toneForValue(row.contribution)}>{row.contribution}</td>
            <td><span className={`risk-state risk-${row.risk}`}>{row.risk}</span></td>
            <td title={row.role}>{row.source}</td>
          </tr>)}</tbody>
        </table>
      </div> : <TerminalEmpty title="暂无持仓" detail="模拟盘形成持仓后，这里会显示市值、组合权重、收益贡献和风险。" />}
    </section>
  )
}

function toneForValue(value: string) {
  return value.trim().startsWith('-') ? 'negative' : value.trim().startsWith('+') ? 'positive' : ''
}
