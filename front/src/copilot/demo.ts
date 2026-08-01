import type { CopilotAnalysis, TradingCopilotState } from './types'

const DEMO_TIME = '2026-08-01T07:00:00.000Z'

export const copilotDemoAnalyses: Record<string, CopilotAnalysis> = {
  '000400.SZ': {
    symbol: '000400.SZ',
    name: '许继电气',
    mode: 'demo_fixture',
    generatedAt: DEMO_TIME,
    score: 74,
    verdict: '等待条件',
    summary: '演示判断：结构偏强，但只有价格、量能与风险条件同时满足后才进入人工计划。',
    support: [
      { title: '趋势结构', detail: '演示数据中，中期趋势保持向上，回撤尚未破坏结构。' },
      { title: '行业线索', detail: '电网设备主题仍有观察价值，需用正式行业证据复核。' },
      { title: '量价确认', detail: '若放量突破观察位，可提升交易条件完整度。' },
    ],
    oppose: [
      { title: '追高风险', detail: '距离演示压力位较近，直接追涨的盈亏比不足。' },
      { title: '数据权限', detail: '当前为页面演示，不是 TradingAgent 的实时覆盖结果。' },
      { title: '组合暴露', detail: '下单前仍需按你的申报持仓检查行业集中度。' },
    ],
    buyConditions: ['收盘站上人工观察位并有量能确认', '预设止损后的单笔风险符合个人上限', '可用现金和行业暴露复核通过'],
    invalidation: ['跌破结构止损位', '放量冲高回落', '正式数据或风险证据不可用'],
  },
  '002594.SZ': {
    symbol: '002594.SZ',
    name: '比亚迪',
    mode: 'demo_fixture',
    generatedAt: DEMO_TIME,
    score: 61,
    verdict: '等待条件',
    summary: '演示判断：关注中，等待趋势与估值风险出现更好的共同条件。',
    support: [{ title: '产业位置', detail: '龙头地位可作为持续跟踪线索。' }],
    oppose: [{ title: '波动与定价', detail: '高波动阶段不适合只凭单一催化追价。' }],
    buyConditions: ['形成可验证的右侧结构', '风险预算和止损距离匹配'],
    invalidation: ['结构破位', '行业风险证据恶化'],
  },
  '601899.SH': {
    symbol: '601899.SH',
    name: '紫金矿业',
    mode: 'demo_fixture',
    generatedAt: DEMO_TIME,
    score: 56,
    verdict: '积极观察',
    summary: '演示判断：适合跟踪商品周期与价格结构，不等于当前买入建议。',
    support: [{ title: '周期线索', detail: '商品价格与盈利弹性可形成研究驱动。' }],
    oppose: [{ title: '周期反转', detail: '宏观和商品价格反转会快速改变论点。' }],
    buyConditions: ['商品与股价方向共同确认', '回撤风险在计划范围内'],
    invalidation: ['商品趋势反转', '股价跌破结构位'],
  },
  '600519.SH': {
    symbol: '600519.SH',
    name: '贵州茅台',
    mode: 'demo_fixture',
    generatedAt: DEMO_TIME,
    score: 48,
    verdict: '暂不参与',
    summary: '演示判断：基本面研究价值不等于价格条件合适，当前等待更清楚的触发。',
    support: [{ title: '质量线索', detail: '品牌与现金流可进入长期研究框架。' }],
    oppose: [{ title: '机会成本', detail: '缺少价格触发时占用资金的效率较低。' }],
    buyConditions: ['估值与趋势同时改善', '出现明确失效点'],
    invalidation: ['消费证据走弱', '趋势继续恶化'],
  },
}

export function createCopilotDemoState(): TradingCopilotState {
  return {
    schemaVersion: 1,
    ownerId: 'nicholas',
    source: 'user_declared',
    updatedAt: DEMO_TIME,
    account: { declaredCapitalCny: 200000, availableCashCny: 118600, updatedAt: DEMO_TIME },
    holdings: [
      { symbol: '000400.SZ', name: '许继电气', quantity: 1000, sellableQuantity: 1000, averageCost: 29.86, updatedAt: DEMO_TIME },
      { symbol: '601899.SH', name: '紫金矿业', quantity: 1800, sellableQuantity: 1800, averageCost: 18.42, updatedAt: DEMO_TIME },
    ],
    watchlist: Object.values(copilotDemoAnalyses).map(({ symbol, name }) => ({ symbol, name, addedAt: DEMO_TIME })),
    decisions: [],
  }
}

export function unavailableAnalysis(symbol: string, name: string): CopilotAnalysis {
  return {
    symbol,
    name,
    mode: 'analysis_unavailable',
    generatedAt: null,
    score: null,
    verdict: '暂无分析',
    summary: '这只股票已加入关注，但 TradingAgent 当前没有可验证的分析结果。Copilot 不会自动补写结论。',
    support: [],
    oppose: [{ title: '覆盖缺口', detail: '等待 TradingAgent 形成带来源和时间的正式观察证据。' }],
    buyConditions: ['正式分析可用', '数据新鲜度与来源验证通过', '你重新复核资金和持仓风险'],
    invalidation: ['任何关键数据缺失时不形成交易计划'],
  }
}
