import { marketLabels } from '../../data/dashboard'
import { riskClass } from '../../lib/format'
import type { HoldingRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'

export function HoldingsTable({ holdings }: { holdings: HoldingRow[] }) {
  return (
    <div aria-label="持仓表" className="terminal-table holdings-table" role="table">
      <div className="terminal-row terminal-head" role="row">
        <span role="columnheader">标的</span>
        <span role="columnheader">市场</span>
        <span role="columnheader">角色</span>
        <span role="columnheader">仓位/金额</span>
        <span role="columnheader">浮动收益</span>
        <span role="columnheader">风险</span>
      </div>
      {holdings.map((holding, index) => (
        <div className="terminal-row" key={`${holding.symbol}-${holding.role}-${index}`} role="row">
          <div role="cell"><AssetCell symbol={holding.symbol} name={holding.name} /></div>
          <span role="cell">{marketLabels[holding.market]}</span>
          <span role="cell">{holding.role}</span>
          <span role="cell">{holding.weight}</span>
          <span className={holding.pnl.startsWith('-') ? 'red-text' : 'cyan-text'} role="cell">{holding.pnl}</span>
          <span className={`status ${riskClass(holding.risk)}`} role="cell">{holding.risk}</span>
        </div>
      ))}
    </div>
  )
}
