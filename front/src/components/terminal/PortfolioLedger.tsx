import type { PortfolioLedgerRow } from '../../lib/terminalViewModels'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'
import { TerminalDataTable, type TerminalColumn } from './TerminalDataTable'

const columns: TerminalColumn<PortfolioLedgerRow>[] = [
  { key: 'asset', label: '资产', sortable: true, value: (row) => `${row.symbol} ${row.assetName}`, render: (row) => <><strong>{row.symbol}</strong>{row.assetName && <small>{row.assetName}</small>}</> },
  { key: 'market', label: '市场', sortable: true, value: (row) => row.market, render: (row) => row.market },
  { key: 'quantity', label: '数量', sortable: true, value: (row) => Number.parseFloat(row.quantity.replace(/,/g, '')), render: (row) => row.quantity },
  { key: 'averagePrice', label: '均价', sortable: true, value: (row) => numeric(row.averagePrice), render: (row) => row.averagePrice },
  { key: 'markPrice', label: '现价', sortable: true, value: (row) => numeric(row.markPrice), render: (row) => row.markPrice },
  { key: 'costBasis', label: '成本', sortable: true, value: (row) => numeric(row.costBasis), render: (row) => row.costBasis },
  { key: 'marketValue', label: '市值', sortable: true, value: (row) => numeric(row.marketValue), render: (row) => row.marketValue },
  { key: 'weight', label: '权重', sortable: true, value: (row) => numeric(row.weight), render: (row) => <div className="exposure-cell"><span style={{ width: row.weight }}>{row.weight}</span></div> },
  { key: 'dayPnl', label: '当日盈亏', sortable: true, value: (row) => numeric(row.dayPnl), className: 'numeric-tone', render: (row) => <span className={toneForValue(row.dayPnl)}>{row.dayPnl}</span> },
  { key: 'pnl', label: '累计盈亏', sortable: true, value: (row) => numeric(row.pnl), render: (row) => <span className={toneForValue(row.pnl)}>{row.pnl}</span> },
  { key: 'contribution', label: '贡献', sortable: true, value: (row) => numeric(row.contribution), render: (row) => <span className={toneForValue(row.contribution)}>{row.contribution}</span> },
  { key: 'risk', label: '风险', sortable: true, value: (row) => row.risk, render: (row) => <span className={`risk-state risk-${row.risk}`}>{row.risk}</span> },
  { key: 'source', label: '来源', sortable: true, value: (row) => `${row.source} ${row.role}`, render: (row) => <span title={row.role}>{row.source}</span> },
]

export function PortfolioLedger({ rows }: { rows: PortfolioLedgerRow[] }) {
  return (
    <section className="terminal-table-panel portfolio-ledger">
      <TerminalPanelHeader eyebrow="PORTFOLIO LEDGER" meta={`${rows.length} 项`} title="持仓账本" />
      {rows.length ? <TerminalDataTable ariaLabel="持仓账本" columns={columns} rowKey={(row) => row.symbol} rows={rows} /> : <TerminalEmpty title="暂无持仓" detail="模拟盘形成持仓后，这里会显示市值、组合权重、收益贡献和风险。" />}
    </section>
  )
}

function numeric(value: string) {
  const parsed = Number(value.replace(/[^0-9.-]/g, ''))
  return Number.isFinite(parsed) ? parsed : null
}

function toneForValue(value: string) {
  return value.trim().startsWith('-') ? 'negative' : value.trim().startsWith('+') ? 'positive' : ''
}
