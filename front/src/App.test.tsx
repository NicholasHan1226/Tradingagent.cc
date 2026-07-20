import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { tradingAgentReadModelSources } from './api/tradingAgentReadModel'
import App from './App'

describe('App navigation and result-first dashboard', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    vi.spyOn(window, 'setInterval').mockImplementation(() => 0 as unknown as ReturnType<typeof window.setInterval>)
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  function click(element: HTMLElement) {
    act(() => {
      fireEvent.click(element)
    })
  }

  it('uses six result-and-process destinations without decision pages', () => {
    render(<App />)

    expect(document.querySelector('.hyper-shell')).toHaveAttribute('data-build', '20260716-today-paper-run-candidate')
    const navigation = screen.getByRole('navigation', { name: '主导航' })
    expect(within(navigation).getAllByRole('button')).toHaveLength(6)
    expect(within(navigation).getByRole('button', { name: '总览' })).toBeInTheDocument()
    expect(within(navigation).getByRole('button', { name: '过程' })).toBeInTheDocument()
    expect(within(navigation).queryByRole('button', { name: '机会' })).not.toBeInTheDocument()
    expect(within(navigation).queryByRole('button', { name: '决策' })).not.toBeInTheDocument()

    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    expect(within(marketHeader).getByText('运行状态').parentElement).toHaveTextContent('1 运行中')
    expect(within(marketHeader).getByText('已完成').parentElement).toHaveTextContent('1')
    expect(screen.getByRole('navigation', { name: '市场状态带' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '证据健康' })).toBeInTheDocument()
  })

  it('renders the homepage around return, automated process, and chart without decision wording', () => {
    render(<App />)

    expect(screen.getByLabelText('收益结果')).toBeInTheDocument()
    expect(screen.getByLabelText('自动化过程')).toBeInTheDocument()
    expect(screen.getAllByText('收益曲线').length).toBeGreaterThan(0)
    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    expect(within(marketHeader).getByText('运行状态').parentElement).toHaveTextContent('1 运行中')
    expect(within(marketHeader).getByText('已完成').parentElement).toHaveTextContent('1')
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'true')
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '实盘' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('模拟执行').length).toBeGreaterThan(0)
    expect(screen.queryByRole('tablist', { name: '收益区间' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '机会从全市场进入，只把可执行结果留在首页。' })).not.toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '当前运行' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '本轮结果' })).not.toBeInTheDocument()
    expect(screen.queryByText('现在判断')).not.toBeInTheDocument()
    expect(screen.queryByText('看决策')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '总览' })).toBeInTheDocument()
  })

  it('renders one continuous workbench with chart, review rail, and blotter', () => {
    render(<App />)

    const workbench = screen.getByRole('region', { name: '交易工作台' })
    expect(within(workbench).getByRole('region', { name: '收益与目标' })).toBeInTheDocument()
    expect(within(workbench).getByRole('complementary', { name: '当前运行' })).toBeInTheDocument()
    expect(within(workbench).getByRole('tablist', { name: '工作台明细' })).toBeInTheDocument()
    expect(screen.getAllByRole('region', { name: '交易工作台' })).toHaveLength(1)

    const chart = within(workbench).getByRole('img', { name: '模拟盘收益曲线' })
    expect(chart).toHaveAttribute('aria-describedby')
    expect(within(chart).queryByRole('button')).not.toBeInTheDocument()
  })

  it('gates live mode without exposing execution controls', () => {
    render(<App />)

    const marketHeader = screen.getByRole('region', { name: '市场与账户' })
    click(screen.getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toHaveTextContent('实盘待接入')
    expect(screen.getAllByText('模拟盘参考')).toHaveLength(2)
    expect(within(marketHeader).getByText('当前收益').parentElement).toHaveTextContent('待接入')
    expect(within(marketHeader).getByText('模拟盘参考')).toBeInTheDocument()
    expect(screen.queryByText('market_data_missing')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /买|卖|下单|确认交易/ })).not.toBeInTheDocument()

    click(screen.getByRole('button', { name: '收益' }))
    expect(screen.getByRole('region', { name: '实盘接入状态' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: '模拟盘收益曲线' })).not.toBeInTheDocument()
  })

  it('replaces demo signals with TradingAgent snapshot signals when the local API is available', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-04T10:00:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [
              {
                symbol: 'IF2601.CFFEX',
                name: '沪深300期指',
                market: 'CNFutures',
                method: '事件驱动',
                status: 'pending',
                impact: '--',
                confidence: '86%',
                age: '31m',
                reason: '价格和成交量接近走强',
                next: '等待触发条件',
                steps: 5,
              },
              {
                symbol: 'BTC-USDT',
                name: 'Bitcoin',
                market: 'Crypto',
                method: '波动突破',
                status: 'missed',
                impact: '-4.3',
                confidence: '62%',
                age: '3h',
                reason: '风险过高',
                next: '进入复盘',
                steps: 5,
              },
            ],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByRole('complementary', { name: '当前运行' })).toHaveTextContent('IF2601.CFFEX'))
    click(screen.getByRole('tab', { name: '自动复盘 1' }))
    expect(screen.getAllByText('BTC-USDT').length).toBeGreaterThan(0)
  })

  it('does not replace an empty TradingAgent snapshot with demo results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-04T10:00:00.000Z',
            domains: {
              performance: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              signals: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
              risk: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText('等待收益写入')).toBeInTheDocument())
    expect(screen.getAllByText('当前没有运行中的自动过程').length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: '当前没有运行中的自动任务' })).toBeInTheDocument()
    expect(screen.queryByText(/等待新机会 · 转化 0%/)).not.toBeInTheDocument()
    click(screen.getByRole('tab', { name: '持仓 0' }))
    expect(screen.getByText('暂无持仓记录')).toBeInTheDocument()
    click(screen.getByRole('button', { name: '持仓' }))
    expect(screen.getByRole('region', { name: '当前没有模拟持仓' })).toHaveTextContent('当前敞口0 项')
    expect(screen.queryByText('empty')).not.toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('shows independent A-share checkpoints, CNFutures maturity, style samples, and capital deployment evidence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-17T08:05:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-17T08:05:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-17T08:05:00.000Z' },
              holdings: { status: 'ready', updatedAt: '2026-07-17T08:05:00.000Z' },
              decisions: { status: 'ready', updatedAt: '2026-07-17T08:05:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-17T08:05:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [],
            funnelEvents: [],
            marketSummaries: [
              {
                market: 'A-share',
                status: 'ready',
                holdingCount: 3,
                signalCount: 4,
                tradeCount: 3,
                styleCount: 4,
                capitalBase: 50_000,
                capitalAuthorityId: 'ashare-capital-v1',
                authorityGeneration: 1,
                executionLineageId: 'ashare-sim-fresh-20260712-v1',
                capitalUtilizationPct: 36,
                deployedCapitalCny: 18_000,
                availableToReserveCny: 27_000,
                riskUsedCny: 18_000,
                riskLimitCny: 45_000,
                undeployedReasons: [
                  { code: 'no_execution_eligible_candidates', amountCny: 27_000 },
                ],
                source: tradingAgentReadModelSources.ashareMarketCapital,
                headline: 'A股模拟盘',
                detail: '独立 5 万账户',
              },
              {
                market: 'CNFutures',
                status: 'ready',
                holdingCount: 1,
                signalCount: 6,
                tradeCount: 2,
                styleCount: 3,
                capitalBase: 50_000,
                capitalAuthorityId: 'cn-futures-capital-v1',
                authorityGeneration: 1,
                executionLineageId: 'cn-futures-sim-fresh-20260712-v1',
                capitalUtilizationPct: 20,
                deployedCapitalCny: 10_000,
                availableToReserveCny: 15_000,
                riskUsedCny: 10_000,
                riskLimitCny: 25_000,
                undeployedReasons: [{ code: 'coverage_not_mature' }],
                source: tradingAgentReadModelSources.cnFuturesMarketCapital,
                headline: '国内期货模拟盘',
                detail: '独立 5 万账户',
              },
            ],
            ashareSampleKpi: {
              source: 'sample_journal_kpi',
              generatedAt: '2026-07-17T08:00:00.000Z',
              tradeDate: '20260717',
              authorityScope: {
                capitalAuthorityId: 'ashare-capital-v1',
                authorityGeneration: 1,
                executionLineageId: 'ashare-sim-fresh-20260712-v1',
              },
              journalEventCount: 31,
              candidateCount: 12,
              predictionCount: 10,
              observationCounterfactualCount: 8,
              explorationFillCount: 1,
              exploitationFillCount: 2,
              completedRoundTripCount: 3,
              riskRejectCount: 3,
              readyForwardLabelCount: 7,
              pendingForwardLabelCount: 4,
              styles: [
                {
                  styleId: 'trend_breakout',
                  candidateCount: 5,
                  predictionCount: 4,
                  observationCounterfactualCount: 4,
                  explorationFillCount: 1,
                  exploitationFillCount: 1,
                  completedRoundTripCount: 2,
                  readyForwardLabelCount: 5,
                  pendingForwardLabelCount: 1,
                  riskRejectCount: 1,
                  winRate: 0.5,
                  expectancyCny: 18,
                  postCostPnlCny: 36,
                  maxDrawdownCny: 12,
                  rejectionReasons: [{ reason: 'single_name_exposure', count: 1 }],
                },
              ],
              promotionEvidenceReady: false,
              automaticPromotionEnabled: false,
              automaticRiskExpansionEnabled: false,
              realTradingEnabled: false,
            },
            ashareMarketMaturity: {
              source: 'sample_journal_kpi',
              generatedAt: '2026-07-17T08:00:02.000Z',
              tradeDate: '20260717',
              authorityScope: {
                capitalAuthorityId: 'ashare-capital-v1',
                authorityGeneration: 1,
                executionLineageId: 'ashare-sim-fresh-20260712-v1',
              },
              stage: 'day5_review_due',
              totalTradingDays: 5,
              checkpointDue: 5,
              promotionEvidenceReady: false,
              liveTransitionAuthorized: false,
              automaticPromotionEnabled: false,
              automaticRiskExpansionEnabled: false,
              realTradingEnabled: false,
            },
            cnFuturesMarketMaturity: {
              source: 'cn_futures_review_journal+sample_kpi',
              generatedAt: '2026-07-17T08:01:00.000Z',
              tradeDate: '20260717',
              freshStartTradeDate: '20260712',
              authorityScope: {
                capitalAuthorityId: 'cn-futures-capital-v1',
                authorityGeneration: 1,
                executionLineageId: 'cn-futures-sim-fresh-20260712-v1',
              },
              capitalPoolCny: 50_000,
              marginUtilizationLimitCny: 25_000,
              stage: 'collecting_long_horizon',
              simulationTradingDays: ['20260713', '20260714', '20260715', '20260716', '20260717'],
              totalSimulationTradingDays: 5,
              sampleCounts: {
                validSampleCount: 24,
                observationCounterfactualCount: 18,
                counterfactualOnlyCount: 9,
                executionEligibleSampleCount: 6,
                completedRoundTripCount: 2,
                forwardLabelCount: 14,
                pendingForwardLabelCount: 7,
                riskRejectCount: 4,
              },
              coverage: {
                products: ['rb', 'IF'],
                productCount: 2,
                volatilityRegimes: ['normal', 'high'],
                volatilityRegimeCount: 2,
                nightSessionSampleCount: 5,
                rolloverSampleCount: 1,
                marginEvidenceSampleCount: 6,
                feeEvidenceSampleCount: 6,
                slippageEvidenceSampleCount: 6,
                extremeRiskSampleCount: 1,
              },
              performance: {
                winRate: 0.5,
                expectancyCny: 26,
                postCostPnlCny: 52,
                maxDrawdownCny: 20,
                stabilityScore: 0.42,
              },
              blockingReasons: ['product_coverage_insufficient', 'rollover_coverage_insufficient'],
              promotionEvidenceReady: false,
              automaticPromotionEnabled: false,
              automaticRiskExpansionEnabled: false,
              liveTransitionAuthorized: false,
              realTradingEnabled: false,
            },
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    const panel = await screen.findByRole('region', { name: '市场成熟度与样本证据' })
    expect(panel).toHaveTextContent('A股 5万模拟账户')
    expect(panel).toHaveTextContent('模拟第 5 个交易日')
    expect(panel).toHaveTextContent('Day 5 复核')
    expect(panel).toHaveTextContent('trend_breakout')
    expect(panel).toHaveTextContent('候选 5')
    expect(panel).toHaveTextContent('预测 4')
    expect(panel).toHaveTextContent('探索 1')
    expect(panel).toHaveTextContent('利用 1')
    expect(panel).toHaveTextContent('期货 5万模拟账户')
    expect(panel).toHaveTextContent('有效样本 24')
    expect(panel).toHaveTextContent('夜盘 5')
    expect(panel).toHaveTextContent('换月 1')
    expect(panel).toHaveTextContent('product_coverage_insufficient')
    expect(panel).toHaveTextContent('资金利用 36.0%')
    expect(panel).toHaveTextContent('组合风险 ¥18,000 / ¥45,000')
    expect(panel).toHaveTextContent('未部署：no_execution_eligible_candidates')
    expect(panel).toHaveTextContent('资金独立，不跨市场净额')
    expect(panel).toHaveTextContent('仅模拟 · 自动晋级关闭')
  })

  it('does not show demo data when production snapshot is unavailable', async () => {
    vi.stubEnv('VITE_TRADING_AGENT_DEMO_PREVIEW', '0')
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('snapshot unavailable')
    }))

    render(<App />)

    await waitFor(() => expect(screen.getByText('等待收益写入')).toBeInTheDocument())
    expect(screen.getByText('等待接口')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
    expect(screen.queryByText('600519.SH')).not.toBeInTheDocument()
    expect(screen.queryByText('+9.42%')).not.toBeInTheDocument()
  })

  it('shows A-share account facts and strategy sample quality in the return cards', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-06T13:10:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              holdings: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              decisions: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
            },
            performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
            portfolio: {
              pnlAmount: -65,
              returnPct: -0.03,
              capitalBase: 200000,
              targetPct: 8,
              maxDrawdownPct: 0,
              tradeCount: 13,
              pointCount: 1,
              source: 'shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/local_sim_trades.jsonl',
              pnlSource: 'ashare_local_sim_account',
              pnlCurrency: 'CNY',
              realizedPnl: 0,
              unrealizedPnl: -65,
              updatedAt: '2026-07-06T13:10:00.000Z',
              ashareAccount: {
                cashAvailable: 101397.47,
                marketValue: 98537.53,
                accountEquity: 199935,
                accountTotalPnl: -65,
                accountReturnPct: -0.03,
                openPositionCount: 13,
                totalSampleCount: 13,
                validationSampleCount: 13,
                strategySampleValidCount: 0,
                strategyTotalPnl: 0,
                strategyMarketValue: 0,
                strategyOpenPositionCount: 0,
                source: 'shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/local_sim_trades.jsonl',
                updatedAt: '2026-07-06T13:10:00.000Z',
              },
            },
            holdings: [{ symbol: '000001.SZ', name: '000001.SZ', market: 'A-share', weight: '¥7,206', pnl: '-¥5', risk: '正常', role: '模拟盘持仓' }],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('自动化过程').length).toBeGreaterThan(0))
    // All Markets decommissioned: A-share-specific account facts not shown in combined view
    expect(screen.getByText('持仓跟踪')).toBeInTheDocument()
    expect(screen.getAllByText('当前没有运行中的自动过程').length).toBeGreaterThan(0)
    expect(screen.getAllByText('1 个持仓继续跟踪').length).toBeGreaterThan(0)
    expect(screen.getAllByText('承压').length).toBeGreaterThan(0)
    expect(screen.getAllByText('运行空闲').length).toBeGreaterThan(0)
    // Switch to A-share via market tape row
    const aShareRow = screen.getByText('A股').closest('button')
    expect(aShareRow).not.toBeNull()
    click(aShareRow!)
    // Verify market switch — MarketSummaryPanel appears (only for specific markets)
    await waitFor(() => expect(screen.getByLabelText('当前市场摘要')).toBeInTheDocument())
  })

  it('shows a per-market returns chart when a specific market is selected, not an All Markets combined curve', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-11T09:00:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
              signals: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
            },
            performance: [{ day: '现在', simulated: -0.03, target: 8, benchmark: 0, opportunity: 0 }],
            portfolio: {
              pnlAmount: -65,
              returnPct: -0.03,
              capitalBase: 200000,
              targetPct: 8,
              maxDrawdownPct: 0,
              tradeCount: 5,
              pointCount: 1,
              source: 'account',
              pnlCurrency: 'CNY',
              updatedAt: '2026-07-11T09:00:00.000Z',
            },
            marketSummaries: [{
              market: 'A-share',
              status: 'ready',
              runtimeState: 'normal',
              holdingCount: 3,
              signalCount: 0,
              tradeCount: 3,
              styleCount: 1,
              capitalBase: 200000,
              pnlAmount: 6931,
              pnlCurrency: 'CNY',
              returnPct: 3.47,
              maxDrawdownPct: 0,
              source: 'market-summary',
              headline: 'A股',
              detail: 'A股结果',
            }],
            holdings: [],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    // Verify the market tape shows A-share return in the row; All Markets combined is disabled
    await waitFor(() => expect(screen.getAllByText('+3.47%').length).toBeGreaterThan(0))
    click(screen.getByRole('button', { name: '收益' }))

    // All Markets returns page shows the performance curve (single-market, not combined).
    // The chart return is -0.03% (from performance data), not the per-market +3.47%.
    await waitFor(() => expect(screen.getByLabelText('收益曲线摘要')).toHaveTextContent('-0.03%'))
    expect(screen.getByLabelText('收益曲线摘要')).not.toHaveTextContent('+3.47%')
    // Switch to A-share via market tape row to see per-market return
    const aShareRow = screen.getByText('A股').closest('button')
    expect(aShareRow).not.toBeNull()
    click(aShareRow!)
    // Verify market switch — no longer "全市场" in MarketHeader
    await waitFor(() => expect(screen.queryByRole('button', { name: '全市场' })).not.toBeInTheDocument())
  })

  it('shows return range controls on a market-specific returns page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-11T09:00:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
              signals: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-11T09:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-11T09:00:00.000Z' },
            },
            performance: [{ day: '7月3日', timestamp: '20260703', simulated: 1.2, target: 8, benchmark: 0.3, opportunity: -0.4 }, { day: '现在', timestamp: '20260711', simulated: 3.47, target: 8, benchmark: 1.1, opportunity: -0.2 }],
            portfolio: {
              pnlAmount: 6931,
              returnPct: 3.47,
              capitalBase: 200000,
              targetPct: 8,
              maxDrawdownPct: 0,
              tradeCount: 3,
              pointCount: 1,
              source: 'account',
              pnlCurrency: 'CNY',
              updatedAt: '2026-07-11T09:00:00.000Z',
            },
            marketSummaries: [{
              market: 'A-share',
              status: 'ready',
              runtimeState: 'normal',
              holdingCount: 3,
              signalCount: 0,
              tradeCount: 3,
              styleCount: 1,
              capitalBase: 200000,
              pnlAmount: 6931,
              pnlCurrency: 'CNY',
              returnPct: 3.47,
              maxDrawdownPct: 0,
              source: 'market-summary',
              headline: 'A股',
              detail: 'A股结果',
            }],
            holdings: [],
            signals: [],
            funnelEvents: [],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('+3.47%').length).toBeGreaterThan(0))
    // Switch to A-share via header dropdown to see per-market returns with range controls
    click(screen.getByRole('button', { name: '全市场' }))
    click(screen.getByRole('menuitem', { name: /A股/ }))
    expect(screen.queryByRole('tablist', { name: '收益区间' })).not.toBeInTheDocument()
    click(screen.getByRole('button', { name: '收益' }))

    const rangeSwitch = screen.getByRole('tablist', { name: '收益区间' })
    expect(within(rangeSwitch).getByRole('tab', { name: '今日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '7日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '30日' })).toBeInTheDocument()
    expect(within(rangeSwitch).getByRole('tab', { name: '全部' })).toBeInTheDocument()
  })

  it('switches the dashboard to market-specific signals, holdings, and summaries', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-06T13:10:00.000Z',
            domains: {
              performance: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              holdings: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              decisions: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-06T13:10:00.000Z' },
            },
            performance: [{ day: '现在', simulated: 0.4, target: 8, benchmark: 0, opportunity: 0 }],
            holdings: [
              { symbol: '600519.SH', name: '贵州茅台', market: 'A-share', weight: '¥7,206', pnl: '-¥5', risk: '正常', role: '模拟盘持仓' },
              { symbol: 'BTC-USDT', name: 'BTC-USDT', market: 'Crypto', weight: '$1,200', pnl: '+$18', risk: '正常', role: 'Grid 持仓' },
            ],
            signals: [
              {
                symbol: '600519.SH',
                name: '贵州茅台',
                market: 'A-share',
                method: '候选池',
                status: 'pending',
                impact: '--',
                confidence: '--',
                age: '4m',
                reason: '等待确认',
                next: '继续观察',
                steps: 3,
                capitalEvidence: {
                  score: 0.82,
                  netInflow: 12800000,
                  source: 'signal_scores',
                },
              },
              {
                symbol: 'BTC-USDT',
                name: 'BTC-USDT',
                market: 'Crypto',
                method: 'Grid · 买入',
                status: 'executed',
                impact: '成交 $667',
                confidence: '已成交',
                age: '2m',
                reason: '模拟盘成交',
                next: '进入复盘',
                steps: 6,
                stage: '成交',
                stageEvidence: 'replay',
              },
            ],
            funnelEvents: [],
            marketSummaries: [
              {
                market: 'A-share',
                status: 'ready',
                runtimeState: 'strategy_wait',
                executionFault: false,
                holdingCount: 1,
                signalCount: 1,
                tradeCount: 0,
                styleCount: 4,
                activeStyleCount: 3,
                noTradeEvidence: {
                  category: 'capital_plan_defensive',
                  evidenceStatus: 'ready',
                  evidenceGaps: [],
                  universeCount: 3213,
                  candidateCount: 3,
                  orderCount: 0,
                  capitalPlanCapacity: 0,
                  riskMode: 'defensive',
                  allowedBuyCount: 0,
                  strategyCashAvailable: 200000,
                  accountCashAvailable: 82683.89,
                  strategyPositionCount: 0,
                  accountPositionCount: 2,
                  ignoredValidationSampleCount: 2,
                },
                source: 'shared/runtime_test/sim_market_health_latest.json',
                headline: 'A股模拟盘策略等待',
                detail: '无交易：capital_plan_defensive',
              },
              {
                market: 'Crypto',
                status: 'ready',
                runtimeState: 'strategy_wait',
                executionFault: false,
                holdingCount: 1,
                signalCount: 1,
                tradeCount: 1,
                styleCount: 2,
                activeStyleCount: 2,
                pnlAmount: 18,
                returnPct: 0.18,
                maxDrawdownPct: 0.4,
                source: 'shared/review/*/style_comparison.json',
                headline: '加密已有 1 笔模拟成交',
                detail: '收益 +18 · 回报 +0.18% · 风格 2/2',
              },
            ],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('BTC-USDT').length).toBeGreaterThan(0))
    expect(screen.getByLabelText('市场运行状态')).toBeInTheDocument()
    expect(screen.getByText('个股流向')).toBeInTheDocument()
    expect(screen.getByText('资金分 82')).toBeInTheDocument()
    expect(screen.getByText('净流入 +¥1280.00万')).toBeInTheDocument()
    click(screen.getByRole('button', { name: '全市场' }))
    click(screen.getByRole('menuitem', { name: /A股/ }))

    expect(screen.getByLabelText('A股资金状态')).toBeInTheDocument()
    expect(screen.getByText('可用资金')).toBeInTheDocument()
    expect(screen.getByText('¥20.00万')).toBeInTheDocument()
    expect(screen.getByText('账户现金')).toBeInTheDocument()
    expect(screen.getByText('¥8.27万')).toBeInTheDocument()
    expect(screen.getByText('复盘/账户持仓')).toBeInTheDocument()
    expect(screen.getByText('0/2')).toBeInTheDocument()
    expect(screen.getByText('不计入复盘')).toBeInTheDocument()

    click(screen.getByRole('button', { name: 'A股' }))
    click(screen.getByRole('menuitem', { name: /加密/ }))

    expect(screen.getByText('加密正在等更好的入场条件')).toBeInTheDocument()
    expect(screen.getAllByText('BTC-USDT').length).toBeGreaterThan(0)
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })

  it('renders queue projections and completed results without requiring a live opportunity source', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            mode: 'simulated',
            generatedAt: '2026-07-05T10:00:00.000Z',
            domains: {
              performance: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              signals: { status: 'ready', updatedAt: '2026-07-05T10:00:00.000Z' },
              holdings: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              decisions: { status: 'empty', updatedAt: '2026-07-05T10:00:00.000Z' },
              risk: { status: 'ready', updatedAt: '2026-07-05T10:00:00.000Z' },
            },
            performance: [],
            holdings: [],
            signals: [
              {
                symbol: '600519.SH',
                name: '贵州茅台',
                market: 'A-share',
                method: '趋势跟踪',
                status: 'executed',
                impact: '+8.2',
                confidence: '81%',
                age: '12m',
                reason: '模拟成交',
                next: '持仓复盘',
                steps: 6,
                stage: '成交',
                stageEvidence: 'full',
              },
              {
                symbol: 'BTC-USDT',
                name: 'Bitcoin',
                market: 'Crypto',
                method: '波动突破',
                status: 'cancelled',
                impact: '-3.1',
                confidence: '58%',
                age: '20m',
                reason: '风险拒绝',
                next: '放弃',
                steps: 3,
                stage: '拒绝',
                stageEvidence: 'partial',
              },
            ],
            funnelEvents: [
              { id: 'a', symbol: '600519.SH', market: 'A-share', stage: '发现', status: '进入', label: '机会进入', source: 'signal_queue' },
              { id: 'b', symbol: '600519.SH', market: 'A-share', stage: '研判', status: '通过', label: '研究通过', source: 'signal_queue' },
              { id: 'c', symbol: '600519.SH', market: 'A-share', stage: '风控', status: '通过', label: '风控通过', source: 'signal_queue' },
              { id: 'd', symbol: '600519.SH', market: 'A-share', stage: '待确认', status: '等待', label: '待执行', source: 'signal_queue' },
              { id: 'e', symbol: '600519.SH', market: 'A-share', stage: '结果', status: '成交', label: '成交', source: 'sim_ledger' },
              { id: 'f', symbol: 'BTC-USDT', market: 'Crypto', stage: '结果', status: '拦截', label: '放弃', source: 'signal_queue' },
            ],
            sourceRefs: tradingAgentReadModelSources,
          }),
          { status: 200 },
        ),
      ),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByLabelText('最近管道事件')).toBeInTheDocument())
    expect(screen.getAllByText('发现').length).toBeGreaterThan(0)
    expect(screen.getAllByText('研究').length).toBeGreaterThan(0)
    expect(screen.getAllByText('风控').length).toBeGreaterThan(0)
    expect(screen.getAllByText('模拟执行').length).toBeGreaterThan(0)
    expect(screen.getAllByText('结果写回').length).toBeGreaterThan(0)
    const process = screen.getByLabelText('自动化过程')
    expect(process).toHaveTextContent('2 条过程进入')
    expect(process).not.toHaveTextContent('实时运行')
    expect(process).not.toHaveTextContent('1 条安全拦截')
    expect(screen.getByLabelText('最近管道事件')).toHaveTextContent('600519.SH')
  })

  it('switches from the return card into the dedicated live gate and back', () => {
    render(<App />)

    click(screen.getByRole('tab', { name: '已完成 1' }))
    const card = screen.getByLabelText('收益结果')
    click(within(card).getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toHaveTextContent('实盘待接入')
    click(screen.getByRole('button', { name: '返回模拟盘' }))
    expect(within(screen.getByLabelText('收益结果')).getByRole('tab', { name: '模拟盘' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '已完成 1' })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows the Process Book beside its automation inspector', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '过程' }))

    expect(screen.getByRole('region', { name: '过程终端' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: '运行中过程账本' })).toBeInTheDocument()
    expect(within(screen.getByLabelText('过程终端检查器')).getByRole('heading', { name: '过程分布' })).toBeInTheDocument()
    expect(screen.getByText('IF2601.CFFEX')).toBeInTheDocument()
    expect(screen.queryByText('BTC-USDT')).not.toBeInTheDocument()
  })

  it('keeps the reserved live state inside the workbench rather than a dialog', () => {
    render(<App />)

    const card = screen.getByLabelText('收益结果')
    click(within(card).getByRole('tab', { name: '实盘' }))

    expect(screen.getByRole('region', { name: '实盘接入状态' })).toBeInTheDocument()
    expect(screen.queryByLabelText('收益结果')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '实盘接入状态' })).not.toBeInTheDocument()
  })

  it('links a chart event marker to the related process view', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '查看 5月28日 过程' }))

    expect(screen.getByRole('region', { name: '过程终端' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: '运行中过程账本' })).toBeInTheDocument()
  })

  it('renders compact process distribution and completion metrics', () => {
    render(<App />)

    click(screen.getByRole('button', { name: '过程' }))

    const inspector = screen.getByLabelText('过程终端检查器')
    expect(within(inspector).getByRole('heading', { name: '过程分布' })).toBeInTheDocument()
    expect(within(inspector).getByText('结果写回')).toBeInTheDocument()
    expect(within(inspector).getByText('安全拦截')).toBeInTheDocument()
  })

  it('opens the local command palette with the desktop shortcut', () => {
    render(<App />)

    fireEvent.keyDown(window, { key: 'k', metaKey: true })

    expect(screen.getByRole('dialog', { name: '终端命令面板' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '搜索终端命令' })).toHaveFocus()
  })

  it('renders the optional paper-day RunBundle as a non-production read-only summary', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({
        mode: 'simulated',
        generatedAt: '2026-07-16T08:00:00.000Z',
        domains: {
          performance: { status: 'empty', updatedAt: '2026-07-16T08:00:00.000Z' },
          signals: { status: 'empty', updatedAt: '2026-07-16T08:00:00.000Z' },
          holdings: { status: 'empty', updatedAt: '2026-07-16T08:00:00.000Z' },
          decisions: { status: 'ready', updatedAt: '2026-07-16T08:00:00.000Z' },
          risk: { status: 'ready', updatedAt: '2026-07-16T08:00:00.000Z' },
        },
        performance: [],
        holdings: [],
        signals: [],
        funnelEvents: [],
        paperDayRun: {
          environment: 'local_candidate',
          productionVerified: false,
          contractId: 'tradingagent.paper_day_loop.v1',
          runId: 'ashare-paper-day-fixture',
          tradeDate: '2026-07-16',
          status: 'completed',
          currentStage: 'reported',
          completedStageCount: 9,
          totalStageCount: 9,
          dataEvidenceState: 'ready',
          simulationExecutionState: 'eligible',
          candidateCount: 2,
          decisionCount: 1,
          simulatedOrderCount: 1,
          simulatedFillCount: 1,
          noTradeReasons: [],
          riskBlocks: [],
          championManifestSha256: 'c'.repeat(64),
          llmEvidenceState: 'evidence_only',
          source: 'shared/runtime/run_bundles/latest.json',
        },
        sourceRefs: tradingAgentReadModelSources,
      }), { status: 200 })),
    )

    render(<App />)

    const panel = await screen.findByRole('region', { name: '今日自动模拟盘状态' })
    expect(panel).toHaveTextContent('本地候选 · 非生产')
    expect(panel).toHaveTextContent('候选 2')
    expect(panel).toHaveTextContent('模拟成交 1')
    expect(panel).toHaveTextContent('LLM 仅作证据')
    expect(panel).not.toHaveTextContent('生产已验证')
  })
})
