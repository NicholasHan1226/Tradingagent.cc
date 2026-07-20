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
    display: '兼容队列只读投影',
    status: 'partial',
    dashboardSurface: '过程页 / 收益图事件',
    readableSources: [
      'signals/{pending,claimed,running}/*.json',
      'signals/{filled,cancelled,expired,failed,partial}/*.json',
    ],
    note: 'signals/ 是迁移期兼容队列；只作非A股法证投影。未绑定V1 authority的A股 pending 行不进入当前状态，未来由OpportunityLedger只读投影替代。',
  },
  {
    id: 'positions',
    display: '模拟盘持仓',
    status: 'partial',
    dashboardSurface: '持仓页 / 主页持仓摘要',
    readableSources: ['signals/positions/*.json', 'shared/accounting/position_plan.jsonl'],
    note: 'positions 写回为空时可降级读取 position_plan；当前真实目录已预留但持仓快照仍待执行器写回。',
  },
  {
    id: 'performance',
    display: '收益结果',
    status: 'partial',
    dashboardSurface: '主页收益曲线 / 收益页',
    readableSources: ['shared/review/daily/daily_brief.jsonl', 'shared/review/daily/midday_review.jsonl', 'signals/filled/*.json', 'shared/accounting/position_plan.jsonl'],
    note: '收益曲线可先从复盘和成交写回聚合；完整净值仍需要资金/持仓连续快照。',
  },
  {
    id: 'decisions',
    display: '过程结果',
    status: 'partial',
    dashboardSurface: '过程页完成漏斗 / 复盘页',
    readableSources: ['shared/review/daily/daily_brief.jsonl', 'shared/review/attribution/strategy_attribution.jsonl', 'shared/review/strategies/strategy_version.jsonl'],
    note: '过程页展示从发现到结果写回的聚合；归因 JSONL 已声明但尚未进入 snapshot 构建。',
  },
  {
    id: 'risk',
    display: '风险边界',
    status: 'ready',
    dashboardSurface: '风险页 / 主页风险摘要',
    readableSources: ['shared/risk/risk_limits.yaml'],
    note: '统一风险限制可用于回撤、保护金额和人工介入提示；当前 snapshot 仍需补充 risk_limits 读取。',
  },
  {
    id: 'execution-readiness',
    display: '分市场执行边界',
    status: 'gated',
    dashboardSurface: '执行状态 / 实盘待接入',
    readableSources: [
      'shared/governance/system_state_matrix.yaml',
      'shared/governance/market_lanes.yaml',
      'shared/logs/execution_lineages/<execution_lineage_id>/*',
    ],
    note: '只读展示A股、CNFutures、Crypto各自的模拟合同与未来适配器状态；不得复用旧执行schema、触发订单或把模拟/Testnet升级为Live。',
  },
]
