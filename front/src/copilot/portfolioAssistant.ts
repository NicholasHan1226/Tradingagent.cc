import type { StockIntelligence } from './stockIntelligence.ts'
import type { TradingCopilotState } from './types.ts'

export type PortfolioCheckLevel = 'ok' | 'attention' | 'blocked'

export type PortfolioCheck = {
  id: 'account_consistency' | 'single_stock_concentration' | 'industry_concentration' | 't_plus_one' | 'plan_risk_budget'
  level: PortfolioCheckLevel
  title: string
  detail: string
}

export type PortfolioAssistantReport = {
  costBasisCny: number
  declaredCapitalCny: number
  availableCashCny: number
  declaredUtilizationPct: number | null
  maxSingleStockPct: number | null
  maxSingleStockSymbol: string | null
  sellableQuantity: number
  totalQuantity: number
  pendingPlanCount: number
  uncappedPlanCount: number
  pendingRiskBudgetCny: number
  industryExposure: Array<{ industry: string; costBasisCny: number; sharePct: number | null; symbols: string[] }>
  checks: PortfolioCheck[]
}

export function buildPortfolioAssistantReport(
  state: TradingCopilotState,
  intelligence: Record<string, StockIntelligence>,
): PortfolioAssistantReport {
  const capital = state.account.declaredCapitalCny
  const cash = state.account.availableCashCny
  const costBasis = state.holdings.reduce((sum, item) => sum + item.quantity * item.averageCost, 0)
  const totalQuantity = state.holdings.reduce((sum, item) => sum + item.quantity, 0)
  const sellableQuantity = state.holdings.reduce((sum, item) => sum + item.sellableQuantity, 0)
  const pendingPlans = state.decisions.filter((item) => item.action === 'planned' && (!item.review || item.review.status === 'pending'))
  const pendingRiskBudget = pendingPlans.reduce((sum, item) => sum + (item.plan?.maxRiskCny ?? 0), 0)
  const uncappedPlanCount = pendingPlans.filter((item) => item.plan?.maxRiskCny === null || item.plan?.maxRiskCny === undefined).length

  const positions = state.holdings.map((item) => ({ symbol: item.symbol, cost: item.quantity * item.averageCost }))
  const largest = positions.toSorted((left, right) => right.cost - left.cost)[0]
  const maxSingleStockPct = largest && capital > 0 ? (largest.cost / capital) * 100 : null
  const industryMap = new Map<string, { costBasisCny: number; symbols: string[] }>()
  let unclassifiedCostBasisCny = 0
  const unclassifiedSymbols: string[] = []
  for (const position of positions) {
    const industry = intelligence[position.symbol]?.company?.industry?.trim()
    if (!industry || industry === '未交付') {
      unclassifiedCostBasisCny += position.cost
      unclassifiedSymbols.push(position.symbol)
      continue
    }
    const current = industryMap.get(industry) ?? { costBasisCny: 0, symbols: [] }
    current.costBasisCny += position.cost
    current.symbols.push(position.symbol)
    industryMap.set(industry, current)
  }
  const industryExposure = [...industryMap.entries()].map(([industry, value]) => ({
    industry,
    costBasisCny: value.costBasisCny,
    sharePct: capital > 0 ? (value.costBasisCny / capital) * 100 : null,
    symbols: value.symbols,
  })).toSorted((left, right) => right.costBasisCny - left.costBasisCny)
  if (unclassifiedSymbols.length) industryExposure.push({
    industry: '行业待正式投影补全',
    costBasisCny: unclassifiedCostBasisCny,
    sharePct: null,
    symbols: unclassifiedSymbols,
  })
  const largestIndustry = industryExposure[0]

  const checks: PortfolioCheck[] = [
    {
      id: 'account_consistency',
      level: capital <= 0 ? 'blocked' : cash > capital || costBasis + cash > capital * 1.02 ? 'attention' : 'ok',
      title: '申报账户一致性',
      detail: capital <= 0
        ? '尚未申报总资金，无法计算组合占用与风险比例。'
        : cash > capital
          ? '可用现金高于申报总资金，请先与券商账户人工核对。'
          : costBasis + cash > capital * 1.02
            ? '成本金额与现金合计高于申报总资金；成本不等于市值，但该差异需要人工核对。'
            : '申报资金、现金和成本金额未发现明显算术冲突；仍不等于券商实时对账。',
    },
    {
      id: 'single_stock_concentration',
      level: maxSingleStockPct === null ? 'blocked' : maxSingleStockPct > 30 ? 'attention' : 'ok',
      title: '单股成本集中度',
      detail: maxSingleStockPct === null
        ? '缺少有效总资金或持仓，无法计算。'
        : `${largest?.symbol} 按申报成本占总资金 ${maxSingleStockPct.toFixed(1)}%；该比例只用于人工复核，不是系统仓位上限。`,
    },
    {
      id: 'industry_concentration',
      level: unclassifiedSymbols.length > 0 || !largestIndustry || largestIndustry.sharePct === null ? 'blocked' : largestIndustry.sharePct > 45 || largestIndustry.symbols.length >= 3 ? 'attention' : 'ok',
      title: '行业与主题重叠',
      detail: !largestIndustry
        ? '暂无持仓。'
        : unclassifiedSymbols.length > 0
          ? `${unclassifiedSymbols.length} 只持仓缺少已验证行业字段；系统不会把“未交付”误算为同一行业。`
          : largestIndustry.sharePct === null
            ? '缺少有效总资金；行业名称仅来自已验证个股投影，未覆盖标的不会被猜测分类。'
          : `${largestIndustry.industry} 涉及 ${largestIndustry.symbols.length} 只，按申报成本占总资金 ${largestIndustry.sharePct.toFixed(1)}%。`,
    },
    {
      id: 't_plus_one',
      level: totalQuantity > 0 && sellableQuantity === 0 ? 'attention' : 'ok',
      title: 'T+1 可卖约束',
      detail: totalQuantity > 0
        ? `当前申报可卖 ${sellableQuantity.toLocaleString('zh-CN')} / 持有 ${totalQuantity.toLocaleString('zh-CN')} 股；可卖数量仍须与券商人工核对。`
        : '暂无持仓，不产生 T+1 可卖约束。',
    },
    {
      id: 'plan_risk_budget',
      level: pendingPlans.length === 0 ? 'ok' : uncappedPlanCount > 0 || pendingRiskBudget > cash ? 'attention' : 'ok',
      title: '人工计划与现金关系',
      detail: pendingPlans.length === 0
        ? '当前没有待执行的人工计划。'
        : `${pendingPlans.length} 个待执行计划合计申报最大风险 ${formatCny(pendingRiskBudget)}；${uncappedPlanCount} 个未填写风险上限。最大风险不是计划投入金额，也不会预约现金。`,
    },
  ]

  return {
    costBasisCny: costBasis,
    declaredCapitalCny: capital,
    availableCashCny: cash,
    declaredUtilizationPct: capital > 0 ? (costBasis / capital) * 100 : null,
    maxSingleStockPct,
    maxSingleStockSymbol: largest?.symbol ?? null,
    sellableQuantity,
    totalQuantity,
    pendingPlanCount: pendingPlans.length,
    uncappedPlanCount,
    pendingRiskBudgetCny: pendingRiskBudget,
    industryExposure,
    checks,
  }
}

function formatCny(value: number) {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 0 }).format(value)
}
