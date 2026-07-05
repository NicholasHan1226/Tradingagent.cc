import { marketLabels } from '../../data/dashboard'
import { riskClass } from '../../lib/format'
import type { HoldingRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'

export function HoldingsTable({ holdings }: { holdings: HoldingRow[] }) {
  return (
    <div className="terminal-table holdings-table">
      <div className="terminal-row terminal-head">
        <span>标的</span>
        <span>市场</span>
        <span>角色</span>
        <span>仓位/金额</span>
        <span>浮动收益</span>
        <span>风险</span>
      </div>
      {holdings.map((holding) => (
        <div className="terminal-row" key={holding.symbol}>
          <AssetCell symbol={holding.symbol} name={holding.name} />
          <span>{marketLabels[holding.market]}</span>
          <span>{holding.role}</span>
          <span>{holding.weight}</span>
          <span className={holding.pnl.startsWith('-') ? 'red-text' : 'cyan-text'}>{holding.pnl}</span>
          <span className={`status ${riskClass(holding.risk)}`}>{holding.risk}</span>
        </div>
      ))}
    </div>
  )
}
