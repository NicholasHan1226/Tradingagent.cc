export type TradingAgentCapabilityStatus = 'ready' | 'partial' | 'gated'

export type TradingAgentCapability = {
  id: string
  display: string
  status: TradingAgentCapabilityStatus
  dashboardSurface: string
  readableSources: string[]
  note: string
}

export const tradingAgentCapabilities: TradingAgentCapability[] = [
  {
    id: 'signals',
    display: '当前机会',
    status: 'ready',
    dashboardSurface: '机会页 / 收益图事件',
    readableSources: ['TradingAgent/signals/pending/*.json', 'TradingAgent/signals/{filled,cancelled,expired,failed,partial}/*.json'],
    note: '信号队列现在已经有真实文件，可先只读展示机会、状态、有效期和下一步。',
  },
  {
    id: 'positions',
    display: '模拟盘持仓',
    status: 'partial',
    dashboardSurface: '持仓页 / 主页持仓摘要',
    readableSources: ['TradingAgent/signals/positions/*.json', 'TradingAgent/shared/accounting/position_plan.jsonl'],
    note: 'positions 写回为空时可降级读取 position_plan；当前真实目录已预留但持仓快照仍待执行器写回。',
  },
  {
    id: 'performance',
    display: '实时收益',
    status: 'partial',
    dashboardSurface: '主页收益曲线 / 收益页',
    readableSources: ['TradingAgent/shared/review/daily/daily_brief.jsonl', 'TradingAgent/signals/filled/*.json', 'TradingAgent/shared/accounting/position_plan.jsonl'],
    note: '收益曲线可先从复盘和成交写回聚合；完整净值仍需要资金/持仓连续快照。',
  },
  {
    id: 'decisions',
    display: '决策结果',
    status: 'partial',
    dashboardSurface: '决策页漏斗 / 复盘页',
    readableSources: ['TradingAgent/shared/review/daily/daily_brief.jsonl', 'TradingAgent/shared/review/attribution/strategy_attribution.jsonl', 'TradingAgent/shared/review/strategies/strategy_version.jsonl'],
    note: '决策页先展示从信号到结果的聚合；更细的路由日志接入后再补逐笔原因。',
  },
  {
    id: 'risk',
    display: '风险边界',
    status: 'ready',
    dashboardSurface: '风险页 / 主页风险摘要',
    readableSources: ['TradingAgent/shared/risk/risk_limits.yaml', 'TradingAgent/shared/risk/pm/pm_risk_report.jsonl'],
    note: '风险限制和风险报告可直接用于回撤、保护金额和人工介入提示。',
  },
  {
    id: 'execution-readiness',
    display: '实盘接入准备',
    status: 'gated',
    dashboardSurface: '实盘预留入口',
    readableSources: ['TradingAgent/shared/execution/signal_card_schema.json', 'TradingAgent/shared/execution/fill_card_schema.json', 'TradingAgent/signals/filled/*.json'],
    note: '只能展示接入状态；不得从 Dashboard 触发实盘执行。',
  },
]
