import type { DepthRow, HoldingRow, Market, Page, PageMeta, PerformancePoint, SignalRow, SignalStatus } from '../types/dashboard'
import type { ApiStatus, DashboardApiResponse } from '../api/types'

export const markets: Market[] = ['All Markets', 'A-share', 'US', 'Crypto', 'HK', 'PM', 'CNFutures']
export const pages: Page[] = ['主页', '收益', '机会', '持仓', '决策', '风险', '复盘']

export const pageMeta: Record<Page, PageMeta> = {
  主页: {
    title: '全市场总览',
    copy: '收益、机会、持仓和风险边界集中在一屏。',
    mode: '总览',
  },
  收益: {
    title: '实时收益与目标',
    copy: '看清收益是否持续、主要靠什么、离目标和回撤边界还有多远。',
    mode: '收益',
  },
  机会: {
    title: '当前机会',
    copy: '只展示现在还能处理的机会、还差什么、风险在哪里。',
    mode: '机会',
  },
  持仓: {
    title: '模拟盘持仓',
    copy: '关注每个持仓赚了多少、占多大、哪里需要留意。',
    mode: '持仓',
  },
  决策: {
    title: '决策影响收益',
    copy: '看研究、交易、风控、组合这些判断最后带来了什么。',
    mode: '决策',
  },
  风险: {
    title: '风险边界',
    copy: '看哪里接近限制、已经避开多少损失、会不会影响目标。',
    mode: '风险',
  },
  复盘: {
    title: '交易复盘',
    copy: '看已关闭机会为什么赚、为什么没做、下次怎么改。',
    mode: '复盘',
  },
}

export const marketLabels: Record<Market, string> = {
  'All Markets': '全市场',
  'A-share': 'A股',
  US: '美股',
  Crypto: '加密',
  HK: '港股',
  PM: '预测',
  CNFutures: '中国期货',
}

export const statusLabels: Record<SignalStatus, string> = {
  executed: '已兑现',
  missed: '未兑现',
  blocked: '已保护',
  pending: '观察中',
  cancelled: '已放弃',
}

export const performanceData: PerformancePoint[] = [
  ['5月6日', 0.2, 0.0, 0.1, -0.2],
  ['5月8日', 1.1, 0.4, 0.2, -0.9],
  ['5月10日', 1.5, 0.8, 0.3, -1.2],
  ['5月12日', 2.4, 1.2, 0.4, -1.4],
  ['5月14日', 2.1, 1.6, 0.5,  -1.8],
  ['5月16日', 3.0, 2.0, 0.7, -2.5],
  ['5月18日', 3.5, 2.5, 0.8, -2.1],
  ['5月20日', 3.8, 3.0, 0.9, -2.4],
  ['5月22日', 4.2, 3.4, 1.0, -2.8],
  ['5月24日', 4.0, 3.8, 1.1, -3.0],
  ['5月26日', 5.0, 4.2, 1.2, -3.6],
  ['5月28日', 6.5, 4.7, 1.3, -4.1],
  ['5月30日', 7.6, 5.2, 1.4, -4.2],
  ['6月1日', 8.2, 5.7, 1.5, -4.0],
  ['6月3日', 8.4, 6.1, 1.6, -3.8],
  ['6月5日', 9.2, 6.5, 1.7, -3.9],
  ['6月7日', 9.8, 6.9, 1.8, -4.2],
  ['6月9日', 10.6, 7.3, 1.9, -3.9],
  ['6月11日', 11.4, 7.7, 2.0, -3.7],
  ['6月13日', 12.6, 8.1, 2.1, -3.5],
  ['6月15日', 12.1, 8.5, 2.0, -3.6],
  ['6月17日', 11.6, 8.8, 2.1, -3.3],
  ['6月19日', 10.9, 9.1, 2.1, -3.0],
  ['6月21日', 11.3, 9.3, 2.1, -2.9],
  ['现在', 9.42, 8.0, 2.15, -2.55],
].map(([day, simulated, target, benchmark, opportunity]) => ({
  day: String(day),
  simulated: Number(simulated),
  target: Number(target),
  benchmark: Number(benchmark),
  opportunity: Number(opportunity),
}))

export const signals: SignalRow[] = [
  {
    symbol: '600519.SH',
    name: '贵州茅台',
    market: 'A-share',
    method: '强势轮动',
    status: 'executed',
    impact: '+18.6',
    confidence: '91%',
    age: '2小时',
    reason: '价格和资金一起走强',
    next: '继续持有，盯住回撤',
    steps: 6,
  },
  {
    symbol: 'AAPL.US',
    name: '苹果',
    market: 'US',
    method: '顺势跟踪',
    status: 'executed',
    impact: '+12.4',
    confidence: '86%',
    age: '3小时',
    reason: '趋势延续，波动仍可控',
    next: '保留仓位，上调目标',
    steps: 6,
  },
  {
    symbol: 'BTC-USD',
    name: '比特币',
    market: 'Crypto',
    method: '突破机会',
    status: 'blocked',
    impact: '+23.7',
    confidence: '74%',
    age: '4小时',
    reason: '机会还在，但波动太大',
    next: '等风险降下来再看',
    steps: 4,
  },
  {
    symbol: 'HYPE-PERP',
    name: 'Hyperliquid 永续',
    market: 'Crypto',
    method: '低位机会',
    status: 'missed',
    impact: '-4.3',
    confidence: '69%',
    age: '3小时',
    reason: '入场条件过严，窗口已过',
    next: '下次少等一个确认条件',
    steps: 5,
  },
  {
    symbol: '0700.HK',
    name: '腾讯',
    market: 'HK',
    method: '事件机会',
    status: 'pending',
    impact: '--',
    confidence: '86%',
    age: '31分钟',
    reason: '财报预期和资金流正在靠近',
    next: '等价格和成交量再走强',
    steps: 5,
  },
  {
    symbol: 'PM-2026',
    name: 'PredictIt 2026',
    market: 'PM',
    method: '利差机会',
    status: 'cancelled',
    impact: '0.0',
    confidence: '62%',
    age: '5小时',
    reason: '收益不再覆盖等待成本',
    next: '本轮不再跟踪',
    steps: 5,
  },
]

export const holdings: HoldingRow[] = [
  { symbol: '600519.SH', name: '贵州茅台', market: 'A-share', weight: '12.8%', pnl: '+$18.4K', risk: '正常', role: '核心收益' },
  { symbol: 'AAPL.US', name: '苹果', market: 'US', weight: '10.6%', pnl: '+$14.7K', risk: '正常', role: '趋势收益' },
  { symbol: '0700.HK', name: '腾讯', market: 'HK', weight: '8.4%', pnl: '+$9.8K', risk: '观察', role: '事件收益' },
  { symbol: 'BTC-USD', name: '比特币', market: 'Crypto', weight: '6.9%', pnl: '-$4.2K', risk: '偏高', role: '波动仓位' },
]

export const signalDepth: DepthRow[] = [
  { label: '已形成收益', value: '195', total: '59.3%', tone: 'cyan' },
  { label: '等待确认', value: '77', total: '23.4%', tone: 'amber' },
  { label: '风险保护', value: '31', total: '9.4%', tone: 'red' },
  { label: '主动放弃', value: '26', total: '7.9%', tone: 'muted' },
]

export const contributionData = [
  { name: '研究', value: 58.4 },
  { name: '交易', value: 41.2 },
  { name: '组合', value: 36.7 },
  { name: '风控', value: -7.1 },
  { name: '落地', value: -0.6 },
]

export const allocationData = [
  { name: 'A股', value: 38 },
  { name: '美股', value: 27 },
  { name: '港股', value: 18 },
  { name: '加密', value: 11 },
  { name: '现金', value: 6 },
]

export function mockDashboardApiResponse(status: ApiStatus = 'ready'): DashboardApiResponse {
  const updatedAt = new Date().toISOString()

  return {
    mode: 'simulated',
    status,
    domains: {
      performance: { status, updatedAt },
      signals: { status, updatedAt },
      holdings: { status, updatedAt },
      decisions: { status, updatedAt },
      risk: { status, updatedAt },
    },
  }
}
