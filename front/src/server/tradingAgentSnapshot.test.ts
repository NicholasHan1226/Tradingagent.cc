import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { describe, expect, it } from 'vitest'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot'

async function createWorkspace() {
  const root = join(tmpdir(), `tad-read-model-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  await mkdir(join(root, 'TradingAgent/shared/accounting'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/shared/review/daily'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/shared/review/opportunities'), { recursive: true })
  await mkdir(join(root, 'signals/pending'), { recursive: true })
  await mkdir(join(root, 'signals/filled'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/signals/positions'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/signals/filled'), { recursive: true })
  return root
}

describe('TradingAgent snapshot reader', () => {
  it('reads position plan, review rows and signal queue without touching live execution', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'TradingAgent/shared/accounting/position_plan.jsonl'),
      JSON.stringify({
        state: 'ok',
        capital_layer: 'simulated',
        positions: [{ ts_code: '0700.HK', quantity: 200, running_cost: 78500, realized_pnl: 9800, thesis: '事件收益' }],
      }) + '\n',
    )
    await writeFile(
      join(root, 'TradingAgent/shared/review/daily/daily_brief.jsonl'),
      JSON.stringify({ trade_date: '2026-07-04', session: 'close', pnl: 1200, win_rate: 0.62 }) + '\n',
    )
    await writeFile(join(root, 'TradingAgent/signals/filled/0700.HK.json'), JSON.stringify({ status: 'filled', ts_code: '0700.HK' }))
    await writeFile(
      join(root, 'signals/pending/0700.HK.json'),
      JSON.stringify({
        order_id: 'sig-0700',
        ts_code: '0700.HK',
        market: 'HK',
        direction: 'buy',
        capital_layer: 'simulated',
        status: 'pending',
        confidence: '86%',
        reason: '财报预期和资金流正在靠近',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.mode).toBe('simulated')
    expect(snapshot.holdings[0]).toMatchObject({ symbol: '0700.HK', market: 'HK', risk: '正常' })
    expect(snapshot.signals[0]).toMatchObject({ symbol: '0700.HK', status: 'pending' })
    expect(snapshot.domains.holdings.status).toBe('ready')
    expect(snapshot.domains.signals.status).toBe('ready')
    expect(snapshot.sourceRefs.positions).toBe('signals/positions/*.json')
    expect(snapshot.sourceRefs.capitalPlan).toBe('shared/accounting/position_plan.jsonl')
  })

  it('can read directly from the TradingAgent project root used by production deployment', async () => {
    const root = join(await createWorkspace(), 'TradingAgent')
    await mkdir(join(root, 'signals/pending'), { recursive: true })

    await writeFile(
      join(root, 'shared/accounting/position_plan.jsonl'),
      JSON.stringify({
        positions: [{ ts_code: '600519.SH', quantity: 100, running_cost: 120000, realized_pnl: 8200 }],
      }) + '\n',
    )
    await writeFile(
      join(root, 'signals/pending/600519.SH.json'),
      JSON.stringify({
        ts_code: '600519.SH',
        market: 'cn',
        status: 'pending',
        scores: {
          capital: 0.82,
          net_mf_amount: 12800000,
        },
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.holdings).toContainEqual(expect.objectContaining({ symbol: '600519.SH', market: 'A-share' }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: '600519.SH',
      status: 'pending',
      capitalEvidence: expect.objectContaining({ score: 0.82, netInflow: 12800000 }),
    }))
  })

  it('keeps mixed signal outcomes visible and normalizes backend market labels', async () => {
    const root = await createWorkspace()
    await mkdir(join(root, 'signals/expired'), { recursive: true })

    for (let index = 0; index < 220; index += 1) {
      await writeFile(
        join(root, `signals/pending/pm-${index}.json`),
        JSON.stringify({
          ts_code: `${544000 + index}`,
          market: 'pm',
          status: 'pending',
        }),
      )
    }
    await writeFile(
      join(root, 'signals/expired/btc.json'),
      JSON.stringify({
        ts_code: 'BTC-USD',
        market: 'crypto',
        status: 'expired',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.signals.length).toBeLessThanOrEqual(240)
    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: 'BTC-USD', market: 'Crypto', status: 'missed' }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      stage: '结果',
      status: '复盘',
      terminal: true,
    }))
    expect(snapshot.signals[0]).toMatchObject({ market: 'PM' })
  })

  it('maps China futures signals and ledger trades to a dedicated dashboard market', async () => {
    const root = await createWorkspace()
    await mkdir(join(root, 'TradingAgent/shared/logs/sim_ledger/cn_futures/index_intraday_directional'), { recursive: true })

    await writeFile(
      join(root, 'signals/pending/IF2601.CFFEX.json'),
      JSON.stringify({
        ts_code: 'IF2601.CFFEX',
        market: 'cn_futures',
        status: 'pending',
      }),
    )
    await writeFile(
      join(root, 'TradingAgent/shared/logs/sim_ledger/cn_futures/index_intraday_directional/trade_journal.jsonl'),
      JSON.stringify({
        fill_price: 3500,
        fill_qty: 1,
        notional: 350000,
        side: 'buy',
        symbol: 'IF2601.CFFEX',
        timestamp: '2026-07-04T11:17:34+00:00',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: 'IF2601.CFFEX', market: 'CNFutures' }))
  })

  it('includes claimed and running cards from the live signal pipeline as active opportunities', async () => {
    const root = await createWorkspace()
    await mkdir(join(root, 'signals/claimed'), { recursive: true })
    await mkdir(join(root, 'signals/running'), { recursive: true })

    await writeFile(
      join(root, 'signals/claimed/0700.HK.json'),
      JSON.stringify({
        ts_code: '0700.HK',
        market: 'HK',
        status: 'claimed',
        scored_at: '2026-07-04T09:44:00.000+08:00',
      }),
    )
    await writeFile(
      join(root, 'signals/running/AAPL.US.json'),
      JSON.stringify({
        ts_code: 'AAPL.US',
        market: 'US',
        status: 'running',
        debated_at: '2026-07-04T09:47:00.000+08:00',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: '0700.HK', status: 'pending', next: '等待执行确认', stage: '待执行' }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: 'AAPL.US', status: 'pending', next: '执行中，等待回执', stage: '待执行' }))
  })

  it('reads return series from the daily review so the homepage can show real performance history', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'TradingAgent/shared/review/daily/daily_brief.jsonl'),
      [
        JSON.stringify({
          trade_date: '2026-07-03',
          simulated_return_pct: 2.4,
          target_return_pct: 1.8,
          benchmark_return_pct: 0.6,
          opportunity_gap_pct: -0.8,
        }),
        JSON.stringify({
          trade_date: '2026-07-04',
          simulated_return_pct: 3.1,
          target_return_pct: 2,
          benchmark_return_pct: 0.7,
          opportunity_gap_pct: -0.5,
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月3日', timestamp: '2026-07-03', simulated: 2.4, target: 1.8, benchmark: 0.6, opportunity: -0.8 },
      { day: '7月4日', timestamp: '2026-07-04', simulated: 3.1, target: 2, benchmark: 0.7, opportunity: -0.5 },
    ])
    expect(snapshot.domains.performance.status).toBe('ready')
  })

  it('reads real simulated PnL series from market style performance trackers', async () => {
    const root = await createWorkspace()
    const performanceRoot = join(root, 'TradingAgent/shared/review/crypto')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(performanceRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 1000,
        positions: {},
      }),
    )

    await writeFile(
      join(performanceRoot, 'style_performance.jsonl'),
      [
        JSON.stringify({
          style_name: 'grid',
          market: 'crypto',
          date: '20260703',
          pnl: 12.5,
          realized_pnl: 0,
          unrealized_pnl: 12.5,
          pnl_source: 'sim_ledger_mark_to_market',
          max_dd: 40,
          trades: 3,
        }),
        JSON.stringify({
          style_name: 'momentum',
          market: 'crypto',
          date: '20260703',
          pnl: -2.5,
          realized_pnl: -2.5,
          unrealized_pnl: 0,
          pnl_source: 'sim_ledger_mark_to_market',
          max_dd: 10,
          trades: 1,
        }),
        JSON.stringify({
          style_name: 'grid',
          market: 'crypto',
          date: '20260704',
          pnl: 7.25,
          realized_pnl: 3,
          unrealized_pnl: 4.25,
          pnl_source: 'sim_ledger_mark_to_market',
          max_dd: 20,
          trades: 2,
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月3日', timestamp: '20260703', simulated: 0.1, target: 4, benchmark: 0, opportunity: -0.4 },
      { day: '现在', timestamp: '20260704', simulated: 0.17, target: 8, benchmark: 0, opportunity: -0.2 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 124.2,
      returnPct: 0.17,
      capitalBase: 72000,
      targetPct: 8,
      maxDrawdownPct: 0.4,
      tradeCount: 2,
      pointCount: 2,
      pnlSource: 'sim_ledger_mark_to_market',
      realizedPnl: 3.6,
      unrealizedPnl: 120.6,
    })
    expect(snapshot.domains.performance.status).toBe('ready')
    expect(snapshot.sourceRefs.performanceTracker).toBe('shared/review/*/style_performance.jsonl')
  })

  it('exposes read-only A-share research evidence for the homepage rail', async () => {
    const root = await createWorkspace()
    await mkdir(join(root, 'TradingAgent/shared/review/ashare'), { recursive: true })
    await writeFile(
      join(root, 'TradingAgent/shared/review/ashare/research_evidence_latest.json'),
      JSON.stringify({
        generated_at: '2026-07-06T09:26:00+08:00',
        trade_date: '20260706',
        read_only: true,
        real_trading_enabled: false,
        opening_auction: {
          state: 'ready',
          phase: 'outside',
          data_mode: 'first_5m_proxy',
          anomaly_count: 1,
          symbols_with_bars: 0,
          proxy_symbols_with_bars: 42,
        },
        closing_momentum: {
          state: 'ready',
          candidate_count: 2,
          symbols_with_bars: 88,
          candidates: [
            {
              symbol: '600000.SH',
              tail_momentum: 0.0123,
              volume_ratio: 4.2,
              label_state: 'pending_next_day_bar',
              next_day_open_return: null,
              next_day_high_return: null,
            },
          ],
        },
        reverse_repo: {
          action: 'lend',
          amount: 12000,
          lots: 12,
          annualized_yield: 0.0205,
          yield_source: 'daily_bar:close',
          estimated_interest: 0.674,
        },
        style_evidence: {
          summary: {
            styles: 7,
            active_sample: 3,
            degraded: 2,
            paused: 1,
            virtual_capital: 200000,
            allocated_capital: 200000,
            unallocated_capital: 0,
          },
        },
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T02:00:00.000Z'),
    })

    expect(snapshot.ashareResearchEvidence).toMatchObject({
      tradeDate: '20260706',
      readOnly: true,
      realTradingEnabled: false,
      openingAuction: {
        dataMode: 'first_5m_proxy',
        anomalyCount: 1,
        proxySymbolsWithBars: 42,
      },
      reverseRepo: {
        amount: 12000,
        annualizedYield: 0.0205,
        yieldSource: 'daily_bar:close',
      },
      styleEvidence: {
        summary: {
          styles: 7,
          virtualCapital: 200000,
          allocatedCapital: 200000,
        },
      },
    })
    expect(snapshot.ashareResearchEvidence?.closingMomentum.candidates[0]).toMatchObject({
      symbol: '600000.SH',
      labelState: 'pending_next_day_bar',
    })
    expect(snapshot.sourceRefs.ashareResearchEvidence).toBe('shared/review/ashare/research_evidence_latest.json')
  })

  it('expands style performance into a trade-timed return curve when ledger timestamps are available', async () => {
    const root = await createWorkspace()
    const performanceRoot = join(root, 'TradingAgent/shared/review/crypto')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(performanceRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 1000,
        positions: {},
      }),
    )

    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          fill_price: 100,
          fill_qty: 1,
          notional: 100,
          side: 'buy',
          symbol: 'BTCUSDT',
          timestamp: '2026-07-03T01:00:00+00:00',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          fill_price: 200,
          fill_qty: 1,
          notional: 100,
          side: 'buy',
          symbol: 'ETHUSDT',
          timestamp: '2026-07-03T02:00:00+00:00',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          fill_price: 300,
          fill_qty: 1,
          notional: 100,
          side: 'buy',
          symbol: 'SOLUSDT',
          timestamp: '2026-07-04T01:30:00+00:00',
        }),
      ].join('\n') + '\n',
    )

    await writeFile(
      join(performanceRoot, 'style_performance.jsonl'),
      [
        JSON.stringify({
          style_name: 'grid',
          market: 'crypto',
          date: '20260703',
          pnl: 10,
          max_dd: 20,
          trades: 2,
        }),
        JSON.stringify({
          style_name: 'grid',
          market: 'crypto',
          date: '20260704',
          pnl: 20,
          max_dd: 10,
          trades: 1,
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月3日 09:00', timestamp: '2026-07-03T01:00:00+00:00', simulated: 0.05, target: 2.67, benchmark: 0, opportunity: -0.1 },
      { day: '7月3日 10:00', timestamp: '2026-07-03T02:00:00+00:00', simulated: 0.1, target: 5.33, benchmark: 0, opportunity: -0.1 },
      { day: '现在', timestamp: '2026-07-04T01:30:00+00:00', simulated: 0.3, target: 8, benchmark: 0, opportunity: -0.1 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 216,
      returnPct: 0.3,
      pointCount: 3,
    })
  })

  it('prefers simulated equity snapshots as the primary realtime performance source', async () => {
    const root = await createWorkspace()
    const equityRoot = join(root, 'TradingAgent/shared/review/portfolio')
    const performanceRoot = join(root, 'TradingAgent/shared/review/crypto')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(equityRoot, { recursive: true })
    await mkdir(performanceRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(equityRoot, 'equity_snapshots.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T09:30:00+08:00',
          total_equity: 100800,
          capital_base: 100000,
          realized_pnl: 300,
          unrealized_pnl: 500,
          target_return_pct: 1.2,
          benchmark_return_pct: 0.1,
          opportunity_gap_pct: -0.4,
          max_drawdown_pct: 0.5,
          trade_count: 4,
          pnl_source: 'equity_snapshot',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:00+08:00',
          total_equity: 102500,
          capital_base: 100000,
          realized_pnl: 1000,
          unrealized_pnl: 1500,
          target_return_pct: 1.6,
          benchmark_return_pct: 0.3,
          opportunity_gap_pct: -0.2,
          max_drawdown_pct: 0.8,
          trade_count: 7,
          pnl_source: 'equity_snapshot',
        }),
        JSON.stringify({
          capital_layer: 'real',
          timestamp: '2026-07-04T10:30:00+08:00',
          total_equity: 999999,
          capital_base: 100000,
          pnl: 899999,
        }),
      ].join('\n') + '\n',
    )

    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 1000,
        positions: {},
      }),
    )
    await writeFile(
      join(performanceRoot, 'style_performance.jsonl'),
      JSON.stringify({
        style_name: 'grid',
        market: 'crypto',
        date: '20260704',
        pnl: 7.25,
        max_dd: 20,
        trades: 2,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月4日 09:30', timestamp: '2026-07-04T09:30:00+08:00', simulated: 0.8, target: 1.2, benchmark: 0.1, opportunity: -0.4 },
      { day: '现在', timestamp: '2026-07-04T10:00:00+08:00', simulated: 2.5, target: 1.6, benchmark: 0.3, opportunity: -0.2 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 2500,
      returnPct: 2.5,
      capitalBase: 100000,
      maxDrawdownPct: 0.8,
      tradeCount: 7,
      pointCount: 2,
      source: expect.stringContaining('daily_mark_to_market'),
      pnlSource: 'equity_snapshot',
      realizedPnl: 1000,
      unrealizedPnl: 1500,
    })
  })

  it('keeps intraday simulated ledger snapshots as a live performance curve', async () => {
    const root = await createWorkspace()
    const cryptoLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    const usLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/us/momentum')
    await mkdir(cryptoLedgerRoot, { recursive: true })
    await mkdir(usLedgerRoot, { recursive: true })

    await writeFile(
      join(cryptoLedgerRoot, 'daily_mark_to_market.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-03T10:00:00+08:00',
          date: '20260703',
          capital_base: 1000,
          total_pnl: 10,
          realized_pnl: 3,
          unrealized_pnl: 7,
          max_drawdown_pct: 0.3,
          target_return_pct: 8,
          trade_count: 1,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:01+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 20,
          realized_pnl: 8,
          unrealized_pnl: 12,
          max_drawdown_pct: 0.6,
          target_return_pct: 8,
          trade_count: 2,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:06:01+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 30,
          realized_pnl: 14,
          unrealized_pnl: 16,
          max_drawdown_pct: 0.7,
          target_return_pct: 8,
          trade_count: 4,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
      ].join('\n') + '\n',
    )
    await writeFile(
      join(usLedgerRoot, 'daily_mark_to_market.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-03T10:00:05+08:00',
          date: '20260703',
          capital_base: 2000,
          total_pnl: -4,
          realized_pnl: -2,
          unrealized_pnl: -2,
          max_drawdown_pct: 0.8,
          target_return_pct: 8,
          trade_count: 1,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:09+08:00',
          date: '20260704',
          capital_base: 2000,
          total_pnl: -5,
          realized_pnl: -1,
          unrealized_pnl: -4,
          max_drawdown_pct: 1.2,
          target_return_pct: 8,
          trade_count: 3,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:06:10+08:00',
          date: '20260704',
          capital_base: 2000,
          total_pnl: -9,
          realized_pnl: -3,
          unrealized_pnl: -6,
          max_drawdown_pct: 1.4,
          target_return_pct: 8,
          trade_count: 4,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月3日 10:00', timestamp: '2026-07-03T10:00:05+08:00', simulated: 0.03, target: 2.67, benchmark: 0, opportunity: 0 },
      { day: '7月4日 10:00', timestamp: '2026-07-04T10:00:09+08:00', simulated: 0.07, target: 5.33, benchmark: 0, opportunity: 0 },
      { day: '现在', timestamp: '2026-07-04T10:06:10+08:00', simulated: 0.1, target: 8, benchmark: 0, opportunity: 0 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 151.2,
      returnPct: 0.1,
      capitalBase: 144000,
      maxDrawdownPct: 1.4,
      tradeCount: 8,
      pointCount: 3,
      pnlSource: 'sim_ledger_mark_to_market',
      realizedPnl: 79.2,
      unrealizedPnl: 72,
    })
  })

  it('forward-fills missing simulated ledger sources so all-market returns keep one capital base', async () => {
    const root = await createWorkspace()
    const cryptoLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    const usLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/us/momentum')
    await mkdir(cryptoLedgerRoot, { recursive: true })
    await mkdir(usLedgerRoot, { recursive: true })

    await writeFile(
      join(cryptoLedgerRoot, 'daily_mark_to_market.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:01+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 10,
          target_return_pct: 8,
          trade_count: 1,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:06:01+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 20,
          target_return_pct: 8,
          trade_count: 2,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:12:01+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 30,
          target_return_pct: 8,
          trade_count: 3,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
      ].join('\n') + '\n',
    )
    await writeFile(
      join(usLedgerRoot, 'daily_mark_to_market.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:05+08:00',
          date: '20260704',
          capital_base: 2000,
          total_pnl: -4,
          target_return_pct: 8,
          trade_count: 1,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:12:10+08:00',
          date: '20260704',
          capital_base: 2000,
          total_pnl: -9,
          target_return_pct: 8,
          trade_count: 2,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([
      { day: '7月4日 10:00', timestamp: '2026-07-04T10:00:05+08:00', simulated: 0.03, target: 2.67, benchmark: 0, opportunity: 0 },
      { day: '7月4日 10:06', timestamp: '2026-07-04T10:06:01+08:00', simulated: 0.08, target: 5.33, benchmark: 0, opportunity: 0 },
      { day: '现在', timestamp: '2026-07-04T10:12:10+08:00', simulated: 0.1, target: 8, benchmark: 0, opportunity: 0 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 151.2,
      returnPct: 0.1,
      capitalBase: 144000,
      tradeCount: 5,
    })
  })

  it('keeps capital-base rebase history normal with the market-aware default floor', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    const pnlSeries = [0, 900, 980, 990, 1000, 995, 292, 294]
    const timestamps = [
      '2026-07-04T10:00:00+08:00',
      '2026-07-04T10:15:00+08:00',
      '2026-07-04T10:30:00+08:00',
      '2026-07-04T10:45:00+08:00',
      '2026-07-04T11:00:00+08:00',
      '2026-07-04T11:15:00+08:00',
      '2026-07-04T11:30:00+08:00',
      '2026-07-04T11:45:00+08:00',
    ]
    await writeFile(
      join(ledgerRoot, 'daily_mark_to_market.jsonl'),
      pnlSeries.map((total_pnl, index) => JSON.stringify({
        capital_layer: 'simulated',
        timestamp: timestamps[index],
        date: '20260704',
        capital_base: 1000,
        total_pnl,
        target_return_pct: 8,
        trade_count: index + 1,
        pnl_source: 'sim_ledger_mark_to_market',
      })).join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.performance.map((point) => point.quality ?? 'normal')).toEqual(Array(8).fill('normal'))
    expect(snapshot.performance[1]).toMatchObject({
      simulated: 9,
      target: 2,
    })
    expect(snapshot.performance.at(-1)).toMatchObject({
      day: '现在',
      simulated: 2.94,
      target: 8,
    })
  })

  it('uses only the canonical A-share server-local ledger for dashboard equity', async () => {
    const root = await createWorkspace()
    const legacyAshareRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/ashare/aggressive')
    const canonicalAshareRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/ashare/ashare_sim')
    await mkdir(legacyAshareRoot, { recursive: true })
    await mkdir(canonicalAshareRoot, { recursive: true })

    await writeFile(
      join(legacyAshareRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-04T10:00:00+08:00',
        date: '20260704',
        capital_base: 16666.7,
        total_pnl: -161.1,
        trade_count: 1,
        pnl_source: 'legacy_ashare_style_ledger',
      }) + '\n',
    )
    await writeFile(
      join(canonicalAshareRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-04T10:00:01+08:00',
        date: '20260704',
        capital_base: 200000,
        total_pnl: 0,
        trade_count: 0,
        pnl_source: 'ashare_local_sim_mark_to_market',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.portfolio).toMatchObject({
      capitalBase: 200000,
      pnlAmount: 0,
      pnlSource: 'ashare_local_sim_mark_to_market',
      returnPct: 0,
      tradeCount: 0,
    })
  })

  it('excludes maintenance simulated ledger rows from dashboard returns and trades', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:00+08:00',
          order_id: 'SIM-PROD-BTCUSDT-buy-grid',
          symbol: 'BTCUSDT',
          side: 'buy',
          fill_qty: 1,
          fill_price: 100,
          notional: 100,
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          exclude_from_dashboard: true,
          run_context: 'maintenance_backfill',
          timestamp: '2026-07-04T10:01:00+08:00',
          order_id: 'SIM-MAINT-ETHUSDT-buy-grid',
          symbol: 'ETHUSDT',
          side: 'buy',
          fill_qty: 1,
          fill_price: 200,
          notional: 200,
        }),
      ].join('\n') + '\n',
    )

    await writeFile(
      join(ledgerRoot, 'daily_mark_to_market.jsonl'),
      [
        JSON.stringify({
          capital_layer: 'simulated',
          timestamp: '2026-07-04T10:00:00+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 10,
          realized_pnl: 0,
          unrealized_pnl: 10,
          trade_count: 1,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
        JSON.stringify({
          capital_layer: 'simulated',
          exclude_from_dashboard: true,
          run_context: 'maintenance_backfill',
          timestamp: '2026-07-04T10:01:00+08:00',
          date: '20260704',
          capital_base: 1000,
          total_pnl: 999,
          realized_pnl: 0,
          unrealized_pnl: 999,
          trade_count: 2,
          pnl_source: 'sim_ledger_mark_to_market',
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 72,
      tradeCount: 1,
      capitalBase: 72000,
    })
    expect(snapshot.performance).toHaveLength(1)
    expect(snapshot.performance[0]).toMatchObject({
      simulated: 0.1,
    })
    expect(snapshot.signals.some((signal) => signal.symbol === 'ETHUSDT')).toBe(false)
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      pnlAmount: 72,
      tradeCount: 1,
    }))
  })

  it('excludes quarantined sim ledger positions from holdings and capital base', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 10_000,
        exclude_from_dashboard: true,
        run_context: 'legacy_usd_capital_quarantine',
        positions: {
          BTCUSDT: {
            quantity: 1,
            avg_cost: 100,
            market_id: 'BTC-USD',
            unrealized_pnl: 12,
          },
        },
      }),
    )
    await writeFile(
      join(ledgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-08T06:35:02+00:00',
        date: '20260708',
        capital_base: 10_000,
        total_pnl: -250,
        trade_count: 7,
        pnl_source: 'sim_ledger_mark_to_market',
      }) + '\n',
    )
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-08T06:35:02+00:00',
        symbol: 'BTCUSDT',
        side: 'buy',
        fill_qty: 1,
        fill_price: 100,
        notional: 100,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.holdings.some((holding) => holding.market === 'Crypto')).toBe(false)
    expect(snapshot.signals.some((signal) => signal.market === 'Crypto')).toBe(false)
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      holdingCount: 0,
      capitalBase: 72_000,
      tradeCount: 0,
      pnlAmount: undefined,
    }))
  })

  it('adds A-share local account cash, holdings and sample quality to the portfolio summary', async () => {
    const root = await createWorkspace()
    const localSimRoot = join(root, 'TradingAgent/shared/logs/local_sim')
    await mkdir(localSimRoot, { recursive: true })

    await writeFile(
      join(localSimRoot, 'local_sim_pnl.json'),
      JSON.stringify({
        ashare_sim: {
          cash_available: 101397.47,
          market_value: 98537.53,
          total_pnl: -65,
          positions: {
            '000001.SZ': { quantity: 700 },
            '000002.SZ': { quantity: 1600 },
          },
        },
      }),
    )
    await writeFile(
      join(localSimRoot, 'local_sim_trades.jsonl'),
      [
        JSON.stringify({ market: 'ashare', side: 'buy', status: 'filled', ts_code: '000001.SZ' }),
        JSON.stringify({ market: 'ashare', side: 'buy', status: 'filled', ts_code: '000002.SZ' }),
      ].join('\n') + '\n',
    )
    await writeFile(
      join(root, 'TradingAgent/signals/positions/simulated_ashare_positions.json'),
      JSON.stringify({
        positions: [
          {
            ts_code: '000001.SZ',
            quantity: 700,
            avg_price: 10.3,
            market_value: 7206.57,
            unrealized_pnl: -5,
          },
        ],
      }),
    )
    const ashareLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/ashare/ashare_sim')
    await mkdir(ashareLedgerRoot, { recursive: true })
    await writeFile(
      join(ashareLedgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        date: '20260708',
        timestamp: '2026-07-08T07:15:02+00:00',
        capital_layer: 'simulated',
        real_execution: false,
        capital_base: 200000,
        total_equity: 195921.89,
        total_pnl: -4078.11,
        return_pct: -2.04,
        trade_count: 2,
        pnl_source: 'ashare_local_sim_mark_to_market',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:00:00.000Z'),
    })

    expect(snapshot.portfolio).toMatchObject({
      capitalBase: 200000,
      pnlAmount: -65,
      pnlCurrency: 'CNY',
      returnPct: -0.03,
      tradeCount: 2,
      ashareAccount: {
        accountEquity: 199935,
        cashAvailable: 101397.47,
        marketValue: 98537.53,
        accountTotalPnl: -65,
        totalSampleCount: 2,
        validationSampleCount: 2,
        strategySampleValidCount: 0,
        strategyTotalPnl: 0,
        strategyMarketValue: 0,
      },
    })
    expect(snapshot.holdings).toContainEqual(expect.objectContaining({
      symbol: '000001.SZ',
      weight: '¥7,207',
      pnl: '-¥5',
    }))
  })

  it('treats after-hours A-share local sim fills as validation samples in the dashboard', async () => {
    const root = await createWorkspace()
    const localSimRoot = join(root, 'TradingAgent/shared/logs/local_sim')
    await mkdir(localSimRoot, { recursive: true })

    await writeFile(
      join(localSimRoot, 'local_sim_pnl.json'),
      JSON.stringify({
        ashare_sim: {
          cash_available: 82683.89,
          market_value: 117228,
          total_pnl: -88.11,
          positions: {
            '000623.SZ': { quantity: 3100 },
            '000685.SZ': { quantity: 5200 },
          },
        },
      }),
    )
    await writeFile(
      join(localSimRoot, 'local_sim_trades.jsonl'),
      JSON.stringify({
        market: 'ashare',
        side: 'buy',
        status: 'filled',
        ts_code: '000623.SZ',
        candidate_pool_layer: 'candidate',
        execution_source: 'ashare_candidate_layer',
        created_at: '2026-07-07T08:26:30+00:00',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-07T09:00:00.000Z'),
    })

    expect(snapshot.portfolio?.ashareAccount).toMatchObject({
      totalSampleCount: 1,
      validationSampleCount: 1,
      strategySampleValidCount: 0,
      strategyTotalPnl: 0,
      strategyMarketValue: 0,
    })
  })

  it('surfaces same-day A-share no-trade attribution in market summaries', async () => {
    const root = await createWorkspace()
    const logRoot = join(root, 'TradingAgent/shared/logs')
    await mkdir(logRoot, { recursive: true })
    await writeFile(
      join(logRoot, 'ashare_no_trade_explanations.jsonl'),
      [
        JSON.stringify({
          date: '20260706',
          generated_at: '2026-07-06T10:00:00+08:00',
          no_trade_explanation: {
            category: 'all_rejected_by_risk',
            action: 'review_risk_rejections',
          },
        }),
        JSON.stringify({
          date: '20260707',
          generated_at: '2026-07-07T10:00:00+08:00',
          no_trade_explanation: {
            category: 'no_candidates',
            action: 'check_candidate_pool_thresholds_and_universe_filter',
            counts: { universe: 3213, candidates: 0, orders: 0 },
          },
        }),
      ].join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-07T02:30:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'A-share',
      detail: expect.stringContaining('无交易：候选池暂无达标机会，检查候选池阈值'),
      noTradeEvidence: expect.objectContaining({
        category: 'no_candidates',
        evidenceStatus: 'ready',
        candidateCount: 0,
        orderCount: 0,
      }),
    }))
  })

  it('keeps same-day A-share no-trade evidence visible when historical trades exist', async () => {
    const root = await createWorkspace()
    const logRoot = join(root, 'TradingAgent/shared/logs')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/ashare/ashare_sim')
    await mkdir(logRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })
    await writeFile(
      join(logRoot, 'ashare_no_trade_explanations.jsonl'),
      JSON.stringify({
        date: '20260708',
        generated_at: '2026-07-08T14:57:00+08:00',
        no_trade_explanation: {
          category: 'capital_plan_defensive',
          action: 'check_position_sizing_and_portfolio_constructor',
          counts: { universe: 3213, candidates: 3, orders: 0 },
          candidate_decision_trace: [{ symbol: '600000.SH', drop_reason: 'capital_plan_capacity_zero' }],
          capital_plan_decision: {
            position_capacity: 0,
            target_positions: 0,
            risk_mode: 'defensive',
            available_cash: 200000,
            account_cash_available: 82683.89,
            sample_adjustment: {
              ignored_validation_sample_count: 2,
              strategy_sample_valid_count: 0,
              account_position_count: 2,
              strategy_position_count: 0,
            },
          },
          portfolio_decision: { allowed_buy_count: 0 },
        },
      }) + '\n',
    )
    await writeFile(
      join(ledgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        timestamp: '2026-07-07T15:00:00+08:00',
        market: 'ashare',
        capital_base: 200000,
        total_pnl: 120,
        trade_count: 2,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-08T07:30:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'A-share',
      tradeCount: 2,
      noTradeEvidence: expect.objectContaining({
        category: 'capital_plan_defensive',
        evidenceStatus: 'ready',
        candidateCount: 3,
        orderCount: 0,
        strategyCashAvailable: 200000,
        accountCashAvailable: 82683.89,
        ignoredValidationSampleCount: 2,
        strategyPositionCount: 0,
        accountPositionCount: 2,
      }),
    }))
  })

  it('keeps performance empty with a clear message when only trade logs exist', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        fill_price: 62699.99,
        fill_qty: 0.0106,
        notional: 666.67,
        side: 'buy',
        symbol: 'BTCUSDT',
        timestamp: '2026-07-04T11:17:34+00:00',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.performance).toEqual([])
    expect(snapshot.domains.performance).toMatchObject({
      status: 'empty',
      message: expect.stringContaining('完整收益曲线'),
    })
  })

  it('deduplicates multi-style simulated ledger trades in market-level summaries', async () => {
    const root = await createWorkspace()
    const aggressiveRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/aggressive')
    const balancedRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/balanced')
    await mkdir(aggressiveRoot, { recursive: true })
    await mkdir(balancedRoot, { recursive: true })

    const trade = {
      fill_price: 62699.99,
      fill_qty: 0.0106,
      notional: 666.67,
      side: 'buy',
      symbol: 'BTCUSDT',
      timestamp: '2026-07-04T11:17:34+00:00',
      signal_source: 'explicit_strategy_signal',
      strategy_name: 'crypto_momentum_breakout',
      reason: 'crypto_momentum_breakout: one_bar_return=0.0160, lookback_return=0.0410',
      conviction: 0.7425,
    }
    await writeFile(join(aggressiveRoot, 'trade_journal.jsonl'), JSON.stringify(trade) + '\n')
    await writeFile(join(balancedRoot, 'trade_journal.jsonl'), JSON.stringify(trade) + '\n')
    await writeFile(
      join(aggressiveRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        timestamp: '2026-07-04T11:18:34+00:00',
        date: '20260704',
        capital_base: 1000,
        total_pnl: 10,
        trade_count: 2,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.signals.filter((signal) => signal.market === 'Crypto')).toHaveLength(1)
    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      method: 'Crypto Momentum Breakout · 买入',
      confidence: '74%',
      reason: 'crypto_momentum_breakout: one_bar_return=0.0160, lookback_return=0.0410',
      strategyName: 'crypto_momentum_breakout',
      signalSource: 'explicit_strategy_signal',
    }))
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      signalCount: 1,
      tradeCount: 1,
    }))
  })

  it('preserves signal stage timestamps so the funnel can animate the real decision path', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'signals/pending/0700.HK.json'),
      JSON.stringify({
        ts_code: '0700.HK',
        market: 'HK',
        opportunity_id: 'opp-hk-0700-001',
        status: 'pending',
        expected_alpha_bps: 18.6,
        discovered_at: '2026-07-04T09:41:00.000+08:00',
        scored_at: '2026-07-04T09:44:00.000+08:00',
        risk_checked_at: '2026-07-04T09:49:00.000+08:00',
      }),
    )
    await mkdir(join(root, 'signals/partial'), { recursive: true })
    await writeFile(
      join(root, 'signals/partial/BTC-USD.json'),
      JSON.stringify({
        ts_code: 'BTC-USD',
        market: 'crypto',
        status: 'partial',
        timestamp: '2026-07-04T09:31:00.000+08:00',
        updated_at: '2026-07-04T09:36:00.000+08:00',
        risk_check: {
          passed: false,
          checks: ['波动过高'],
        },
      }),
    )
    await writeFile(
      join(root, 'signals/filled/600519.SH.json'),
      JSON.stringify({
        ts_code: '600519.SH',
        market: 'cn',
        status: 'filled',
        direction: 'buy',
        candidate_pool_layer: 'candidate',
        execution_source: 'ashare_candidate_layer',
        alpha_bps: 24.3,
        discovered_at: '2026-07-04T08:58:00.000+08:00',
        scored_at: '2026-07-04T09:05:00.000+08:00',
        risk_checked_at: '2026-07-04T09:08:00.000+08:00',
        trigger: {
          triggered_at: '2026-07-04T09:12:00.000+08:00',
          trigger_price: 10.22,
        },
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: '0700.HK',
      opportunityId: 'opp-hk-0700-001',
      stage: '风控',
      impact: '+18.6 bps',
      stageTimes: expect.objectContaining({ discovered: '09:41', scored: '09:44', riskChecked: '09:49' }),
    }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: '600519.SH',
      stage: '成交',
      impact: '+24.3 bps',
      stageTimes: expect.objectContaining({ triggered: '09:12' }),
    }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      status: 'blocked',
      stage: '拒绝',
      reason: '风控未通过：波动过高',
      stageTimes: expect.objectContaining({ discovered: '09:31', riskChecked: '09:36' }),
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: '0700.HK',
      opportunityId: 'opp-hk-0700-001',
      sequence: 3,
      stage: '风控',
      status: '通过',
      source: 'signal_queue',
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      stage: '风控',
      status: '拦截',
      source: 'signal_queue',
      terminal: true,
    }))
  })

  it('reads explicit opportunity funnel event logs as the homepage funnel source', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'TradingAgent/shared/review/opportunities/funnel_events.jsonl'),
      [
        {
          opportunity_id: 'opp-0700-breakout',
          event_id: 'opp-0700-breakout-discover',
          ts_code: '0700.HK',
          market: 'hk',
          stage: 'discovered',
          status: 'entered',
          label: '发现机会',
          timestamp: '2026-07-04T09:41:00.000+08:00',
        },
        {
          opportunity_id: 'opp-0700-breakout',
          event_id: 'opp-0700-breakout-research',
          ts_code: '0700.HK',
          market: 'hk',
          stage: 'research',
          status: 'passed',
          latency_minutes: 4,
          timestamp: '2026-07-04T09:45:00.000+08:00',
        },
        {
          opportunityId: 'opp-0700-breakout',
          id: 'opp-0700-breakout-pending',
          symbol: '0700.HK',
          market: 'HK',
          stage: 'pending',
          status: 'waiting',
          reason: '等待价格确认',
          timestamp: '2026-07-04T09:51:00.000+08:00',
        },
        {
          opportunity_id: 'opp-btc-volatility',
          event_id: 'opp-btc-volatility-blocked',
          ts_code: 'BTCUSDT',
          market: 'crypto',
          stage: 'blocked',
          status: 'blocked',
          label: '风险挡住',
          terminal: true,
          timestamp: '2026-07-04T09:58:00.000+08:00',
        },
      ].map((row) => JSON.stringify(row)).join('\n') + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.domains.signals.status).toBe('ready')
    expect(snapshot.sourceRefs.opportunityEvents).toContain('shared/review/opportunities/funnel_events.jsonl')
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: '0700.HK',
      opportunityId: 'opp-0700-breakout',
      stage: '待确认',
      status: '等待',
      source: 'opportunity_log',
      reason: '等待价格确认',
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      opportunityId: 'opp-btc-volatility',
      stage: '结果',
      status: '拦截',
      source: 'opportunity_log',
      terminal: true,
    }))
  })

  it('uses the server-local simulated ledger as the homepage funnel and holdings source', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 10812.35,
        positions: {
          BTCUSDT: { avg_cost: 62891.44, quantity: 0.0186, realized_pnl: 12.5 },
        },
      }),
    )
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        fill_price: 62699.99,
        fill_qty: 0.0106,
        notional: 666.67,
        order_id: 'SIM-2026-07-04-BTCUSDT-buy-grid',
        realized_pnl: 0,
        side: 'buy',
        symbol: 'BTCUSDT',
        timestamp: '2026-07-04T11:17:34+00:00',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.holdings).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      market: 'Crypto',
      role: 'Grid 持仓',
    }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      opportunityId: 'SIM-2026-07-04-BTCUSDT-buy-grid',
      status: 'executed',
      method: 'Grid · 买入',
      stage: '成交',
      stageEvidence: 'replay',
      impact: '成交 ¥4,800',
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      opportunityId: 'SIM-2026-07-04-BTCUSDT-buy-grid',
      stage: '结果',
      status: '成交',
      source: 'sim_ledger',
    }))
    expect(snapshot.performance).toEqual([])
    expect(snapshot.domains.holdings.status).toBe('ready')
    expect(snapshot.domains.signals.status).toBe('ready')
  })

  it('keeps queue and simulated result events in the same opportunity funnel', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(root, 'signals/pending/BTCUSDT.json'),
      JSON.stringify({
        order_id: 'opp-btc-grid-001',
        ts_code: 'BTCUSDT',
        market: 'crypto',
        status: 'pending',
        discovered_at: '2026-07-04T09:30:00.000+08:00',
        scored_at: '2026-07-04T09:34:00.000+08:00',
        risk_checked_at: '2026-07-04T09:38:00.000+08:00',
      }),
    )
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        fill_price: 62699.99,
        fill_qty: 0.0106,
        notional: 666.67,
        order_id: 'opp-btc-grid-001',
        side: 'buy',
        symbol: 'BTCUSDT',
        timestamp: '2026-07-04T02:00:00.000Z',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T03:00:00.000Z'),
    })

    const sameOpportunityEvents = snapshot.funnelEvents.filter((event) => event.opportunityId === 'opp-btc-grid-001')
    expect(sameOpportunityEvents).toContainEqual(expect.objectContaining({ source: 'signal_queue', stage: '发现' }))
    expect(sameOpportunityEvents).toContainEqual(expect.objectContaining({ source: 'sim_ledger', stage: '结果', status: '成交', terminal: true }))
  })

  it('merges signal queue rows with non-A-share simulated ledger rows', async () => {
    const root = await createWorkspace()
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(ledgerRoot, { recursive: true })

    await writeFile(
      join(root, 'signals/filled/600519.SH.json'),
      JSON.stringify({
        ts_code: '600519.SH',
        market: 'ashare',
        status: 'filled',
        direction: 'buy',
        candidate_pool_layer: 'candidate',
        execution_source: 'ashare_candidate_layer',
        fill: { filled_at: '2026-07-06T01:30:00.000Z', filled_price: 1500, filled_qty: 1 },
      }),
    )
    await writeFile(
      join(root, 'TradingAgent/signals/positions/ashare.json'),
      JSON.stringify([{ ts_code: '600519.SH', quantity: 1, market_value: 1500, realized_pnl: 0 }]),
    )
    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 9000,
        positions: {
          ETHUSDT: { avg_cost: 3100, quantity: 0.5, realized_pnl: 18 },
        },
      }),
    )
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        fill_price: 3100,
        fill_qty: 0.5,
        notional: 1550,
        side: 'buy',
        symbol: 'ETHUSDT',
        timestamp: '2026-07-06T02:00:00.000Z',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T03:00:00.000Z'),
    })

    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: '600519.SH', market: 'A-share' }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: 'ETH-USD', market: 'Crypto', stage: '成交' }))
    expect(snapshot.holdings).toContainEqual(expect.objectContaining({ symbol: '600519.SH', market: 'A-share' }))
    expect(snapshot.holdings).toContainEqual(expect.objectContaining({ symbol: 'ETH-USD', market: 'Crypto' }))
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({ market: 'Crypto', signalCount: 1, holdingCount: 1 }))
  })

  it('hides A-share executed signal cards without candidate provenance', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'signals/filled/000001.SZ.json'),
      JSON.stringify({
        ts_code: '000001.SZ',
        market: 'ashare',
        status: 'filled',
        direction: 'buy',
        fill: { filled_at: '2026-07-06T03:13:20.000Z', filled_price: 10, filled_qty: 100 },
      }),
    )
    await writeFile(
      join(root, 'signals/filled/600519.SH.json'),
      JSON.stringify({
        ts_code: '600519.SH',
        market: 'ashare',
        status: 'filled',
        direction: 'buy',
        candidate_pool_layer: 'candidate',
        execution_source: 'ashare_candidate_layer',
        fill: { filled_at: '2026-07-06T03:20:00.000Z', filled_price: 1500, filled_qty: 100 },
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T04:00:00.000Z'),
    })

    expect(snapshot.signals).not.toContainEqual(expect.objectContaining({ symbol: '000001.SZ', market: 'A-share' }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: '600519.SH', market: 'A-share' }))
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({ market: 'A-share', signalCount: 1 }))
  })

  it('reads CNFutures simulated position snapshots from signals/positions', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'TradingAgent/signals/positions/cn_futures_sim_positions.json'),
      JSON.stringify({
        positions: [
          {
            symbol: 'IF2607.CFE',
            style: 'index_intraday_directional',
            net_qty: 1,
            avg_price: 4100,
            mark_price: 4118,
            margin_required: 123000,
            realized_pnl: 800,
            unrealized_pnl: 5400,
          },
        ],
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T02:30:00.000Z'),
    })

    expect(snapshot.holdings).toContainEqual(expect.objectContaining({
      symbol: 'IF2607.CFE',
      market: 'CNFutures',
      role: 'Index Intraday Directional 持仓',
    }))
  })

  it('exposes per-market runtime summaries from style comparison reports', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/crypto')
    await mkdir(reviewRoot, { recursive: true })
    await writeFile(
      join(reviewRoot, 'style_comparison.json'),
      JSON.stringify({
        market: 'crypto',
        capital_layer: 'simulated',
        account_type: 'simulated',
        real_execution: false,
        styles_total: 2,
        styles_loaded: 2,
        style_states: [
          { style_name: 'grid', status: 'active' },
          { style_name: 'momentum', status: 'degraded' },
        ],
        filled_count: 3,
        error_count: 1,
        signal_count: 4,
        generated_at: '2026-07-06T12:35:00.000Z',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      status: 'partial',
      runtimeState: 'needs_attention',
      executionFault: true,
      styleCount: 2,
      activeStyleCount: 1,
      degradedStyleCount: 1,
      filledCount: 3,
      errorCount: 1,
    }))
  })

  it('uses CNFutures latest review as the current runtime source instead of adding style comparison counts', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/cn_futures')
    await mkdir(reviewRoot, { recursive: true })
    await mkdir(join(root, 'TradingAgent/shared/review/data'), { recursive: true })
    await writeFile(
      join(reviewRoot, 'style_comparison.json'),
      JSON.stringify({
        market: 'cn_futures',
        capital_layer: 'simulated',
        account_type: 'simulated',
        real_execution: false,
        styles_total: 4,
        styles_loaded: 4,
        filled_count: 2,
        error_count: 3,
        hold_count: 5,
        generated_at: '2026-07-06T09:30:00.000Z',
      }),
    )
    await writeFile(
      join(root, 'TradingAgent/shared/review/data/cn_futures_sim_reviews.jsonl'),
      JSON.stringify({
        generated_at: '2026-07-09T05:40:00.000Z',
        state: 'ok',
        cadence: '5min',
        filled_count: 0,
        hold_count: 8,
        error_count: 0,
        record_count: 8,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-09T05:45:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'CNFutures',
      runtimeState: 'strategy_wait',
      source: 'shared/review/data/cn_futures_sim_reviews.jsonl',
      styleCount: 1,
      filledCount: 0,
      errorCount: 0,
    }))
  })

  it('excludes quarantined style comparison reports from market summaries', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/crypto')
    await mkdir(reviewRoot, { recursive: true })
    await writeFile(
      join(reviewRoot, 'style_comparison.json'),
      JSON.stringify({
        market: 'crypto',
        capital_layer: 'simulated',
        account_type: 'simulated',
        real_execution: false,
        exclude_from_dashboard: true,
        run_context: 'legacy_usd_capital_quarantine',
        styles_total: 2,
        styles_loaded: 2,
        style_states: [
          { style_name: 'grid', status: 'active' },
          { style_name: 'momentum', status: 'degraded' },
        ],
        filled_count: 3,
        error_count: 1,
        signal_count: 4,
        generated_at: '2026-07-06T12:35:00.000Z',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:40:00.000Z'),
    })

    const cryptoSummary = (snapshot.marketSummaries ?? []).find((summary) => summary.market === 'Crypto')
    expect(cryptoSummary).toBeDefined()
    expect(cryptoSummary).not.toMatchObject({
      styleCount: 2,
      filledCount: 3,
      errorCount: 1,
    })
  })

  it('excludes style performance rows when their sim ledger positions are quarantined', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/us')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/us/swing')
    await mkdir(reviewRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })
    await writeFile(
      join(ledgerRoot, 'positions.json'),
      JSON.stringify({
        cash: 10_000,
        exclude_from_dashboard: true,
        run_context: 'legacy_usd_capital_quarantine',
        positions: {},
      }),
    )
    await writeFile(
      join(reviewRoot, 'style_performance.jsonl'),
      JSON.stringify({
        style_name: 'swing',
        market: 'us',
        date: '20260708',
        capital_layer: 'simulated',
        account_type: 'simulated',
        real_execution: false,
        pnl: -12.5,
        trades: 9,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:40:00.000Z'),
    })

    expect(snapshot.portfolio).toBeUndefined()
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'US',
      pnlAmount: undefined,
      tradeCount: 0,
    }))
  })

  it('marks a market with active styles but no trades as strategy wait', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/pm')
    await mkdir(reviewRoot, { recursive: true })
    await writeFile(
      join(reviewRoot, 'style_comparison.json'),
      JSON.stringify({
        market: 'pm',
        capital_layer: 'simulated',
        account_type: 'simulated',
        real_execution: false,
        styles_total: 1,
        styles_loaded: 1,
        style_states: [{ style_name: 'probability_edge', status: 'active' }],
        filled_count: 0,
        hold_count: 4,
        error_count: 0,
        generated_at: '2026-07-06T12:35:00.000Z',
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'PM',
      status: 'partial',
      runtimeState: 'strategy_wait',
      executionFault: false,
      styleCount: 1,
      filledCount: 0,
    }))
  })

  it('uses CNFutures review samples to avoid showing live reviews as missing data', async () => {
    const root = await createWorkspace()
    const reviewRoot = join(root, 'TradingAgent/shared/review/data')
    await mkdir(reviewRoot, { recursive: true })
    await writeFile(
      join(reviewRoot, 'cn_futures_sim_reviews.jsonl'),
      JSON.stringify({
        state: 'ok',
        generated_at: '2026-07-06T21:35:00+08:00',
        filled_count: 0,
        hold_count: 4,
        error_count: 0,
        record_count: 4,
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T13:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'CNFutures',
      status: 'partial',
      runtimeState: 'strategy_wait',
      executionFault: false,
      styleCount: 1,
      filledCount: 0,
    }))
  })

  it('uses latest simulated health to show current strategy wait even with historical trades', async () => {
    const root = await createWorkspace()
    const healthRoot = join(root, 'TradingAgent/shared/runtime_test')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/aggressive')
    await mkdir(healthRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        market: 'crypto',
        symbol: 'BTCUSDT',
        side: 'buy',
        quantity: 1,
        price: 63000,
        timestamp: '2026-07-07T12:00:00Z',
      }) + '\n',
    )
    await writeFile(
      join(healthRoot, 'sim_market_health_latest.json'),
      JSON.stringify({
        market: 'all_sim',
        checks: [
          {
            name: 'crypto_sim_loop',
            status: 'warn',
            summary: 'crypto 模拟盘闭环策略等待',
            details: {
              market: 'crypto',
              diagnostic_class: 'strategy_wait',
              execution_fault: false,
              warn_reasons: ['crypto_waiting_for_momentum_signal'],
            },
          },
        ],
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-07T12:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      runtimeState: 'strategy_wait',
      executionFault: false,
      runtimeReason: 'crypto_waiting_for_momentum_signal',
      headline: 'Crypto 模拟盘闭环策略等待',
    }))
  })

  it('ignores stale simulated health when building market summaries', async () => {
    const root = await createWorkspace()
    const healthRoot = join(root, 'TradingAgent/shared/runtime_test')
    const ledgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/aggressive')
    await mkdir(healthRoot, { recursive: true })
    await mkdir(ledgerRoot, { recursive: true })
    await writeFile(
      join(ledgerRoot, 'trade_journal.jsonl'),
      JSON.stringify({
        market: 'crypto',
        symbol: 'BTCUSDT',
        side: 'buy',
        quantity: 1,
        price: 63000,
        timestamp: '2026-07-07T12:00:00Z',
      }) + '\n',
    )
    await writeFile(
      join(healthRoot, 'sim_market_health_latest.json'),
      JSON.stringify({
        market: 'all_sim',
        generated_at: '2026-07-07T11:00:00.000Z',
        checks: [
          {
            name: 'crypto_sim_loop',
            status: 'warn',
            summary: 'crypto 模拟盘闭环策略等待',
            details: {
              market: 'crypto',
              diagnostic_class: 'strategy_wait',
              execution_fault: false,
              warn_reasons: ['crypto_waiting_for_momentum_signal'],
            },
          },
        ],
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-07T12:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      runtimeState: 'normal',
      runtimeReason: undefined,
    }))
  })

  it('uses market-aware default capital when no ledger capital is provided', async () => {
    const root = await createWorkspace()
    const usReviewRoot = join(root, 'TradingAgent/shared/review/us')
    const cryptoReviewRoot = join(root, 'TradingAgent/shared/review/crypto')
    const cnFuturesReviewRoot = join(root, 'TradingAgent/shared/review/cn_futures')
    await mkdir(usReviewRoot, { recursive: true })
    await mkdir(cryptoReviewRoot, { recursive: true })
    await mkdir(cnFuturesReviewRoot, { recursive: true })

    for (const [reviewRoot, market] of [
      [usReviewRoot, 'us'],
      [cryptoReviewRoot, 'crypto'],
      [cnFuturesReviewRoot, 'cn_futures'],
    ] as const) {
      await writeFile(
        join(reviewRoot, 'style_comparison.json'),
        JSON.stringify({
          market,
          capital_layer: 'simulated',
          account_type: 'simulated',
          real_execution: false,
          styles_total: 1,
          styles_loaded: 1,
          style_states: [{ style_name: 'momentum', status: 'active' }],
          filled_count: 0,
          error_count: 0,
          generated_at: '2026-07-06T12:35:00.000Z',
        }),
      )
    }

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-06T12:40:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'US',
      capitalBase: 72_000,
    }))
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      capitalBase: 72_000,
    }))
    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'CNFutures',
      capitalBase: 50_000,
    }))
  })

  it('normalizes USD market ledger capitalBase to the canonical 10000 USD equivalent', async () => {
    const root = await createWorkspace()
    const cryptoLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    await mkdir(cryptoLedgerRoot, { recursive: true })

    await writeFile(
      join(cryptoLedgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-04T10:00:00+08:00',
        date: '20260704',
        capital_base: 5_000,
        total_pnl: 500,
        target_return_pct: 8,
        trade_count: 1,
        pnl_source: 'sim_ledger_mark_to_market',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.marketSummaries).toContainEqual(expect.objectContaining({
      market: 'Crypto',
      capitalBase: 72_000,
      pnlAmount: 3_600,
      returnPct: 5,
    }))
  })

  it('uses the sum of market defaults for the all-markets portfolio floor', async () => {
    const root = await createWorkspace()
    const cryptoLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/crypto/grid')
    const usLedgerRoot = join(root, 'TradingAgent/shared/logs/sim_ledger/us/momentum')
    await mkdir(cryptoLedgerRoot, { recursive: true })
    await mkdir(usLedgerRoot, { recursive: true })

    await writeFile(
      join(cryptoLedgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-04T10:00:00+08:00',
        date: '20260704',
        capital_base: 1_000,
        total_pnl: 10,
        target_return_pct: 8,
        trade_count: 1,
        pnl_source: 'sim_ledger_mark_to_market',
      }) + '\n',
    )
    await writeFile(
      join(usLedgerRoot, 'daily_mark_to_market.jsonl'),
      JSON.stringify({
        capital_layer: 'simulated',
        timestamp: '2026-07-04T10:00:00+08:00',
        date: '20260704',
        capital_base: 2_000,
        total_pnl: -5,
        target_return_pct: 8,
        trade_count: 1,
        pnl_source: 'sim_ledger_mark_to_market',
      }) + '\n',
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.portfolio).toMatchObject({
      capitalBase: 144_000,
      pnlAmount: 36,
      returnPct: 0.03,
    })
  })

  it('exposes A-share main and capital-tier summaries for dashboard comparison', async () => {
    const root = await createWorkspace()
    const ashareReview = join(root, 'TradingAgent/shared/review/ashare')
    const tiersRoot = join(root, 'TradingAgent/shared/logs/local_sim_tiers')
    const localSimDir = join(root, 'TradingAgent/shared/logs/local_sim')
    await mkdir(ashareReview, { recursive: true })
    await mkdir(join(tiersRoot, 'ashare_50000'), { recursive: true })
    await mkdir(join(tiersRoot, 'ashare_100000'), { recursive: true })
    await mkdir(localSimDir, { recursive: true })

    await writeFile(
      join(localSimDir, 'local_sim_pnl.json'),
      JSON.stringify({
        ashare_server_sim: {
          cash_available: 50_200,
          market_value: 151_000,
          total_pnl: 1_200,
        },
      }),
    )
    await writeFile(
      join(localSimDir, 'local_sim_trades.jsonl'),
      Array.from({ length: 5 }, (_, index) =>
        JSON.stringify({ market: 'ashare', status: 'filled', ts_code: `60000${index}.SH`, side: 'buy' }),
      ).join('\n') + '\n',
    )
    await writeFile(
      join(ashareReview, 'tier_experiments_latest.json'),
      JSON.stringify({
        market: 'ashare',
        accounts: [
          { account: 'ashare_50000', capital: 50_000, trade_count: 3 },
          { account: 'ashare_100000', capital: 100_000, trade_count: 4 },
        ],
      }),
    )
    await writeFile(
      join(tiersRoot, 'ashare_50000/local_sim_pnl.json'),
      JSON.stringify({
        ashare_50000: {
          cash_available: 13_300,
          market_value: 37_000,
          total_pnl: 300,
          total_trades: 3,
        },
      }),
    )
    await writeFile(
      join(tiersRoot, 'ashare_100000/local_sim_pnl.json'),
      JSON.stringify({
        ashare_100000: {
          cash_available: 27_700,
          market_value: 76_000,
          total_pnl: 700,
          total_trades: 4,
        },
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.ashareTierSummaries).toHaveLength(3)
    expect(snapshot.ashareTierSummaries?.[0]).toMatchObject({
      account: 'ashare_server_sim',
      capital: 200_000,
      totalPnl: 1_200,
      returnPct: 0.6,
      tradeCount: 5,
    })
    expect(snapshot.ashareTierSummaries?.[1]).toMatchObject({
      account: 'ashare_50000',
      capital: 50_000,
      totalPnl: 300,
      returnPct: 0.6,
      tradeCount: 3,
    })
    expect(snapshot.ashareTierSummaries?.[2]).toMatchObject({
      account: 'ashare_100000',
      capital: 100_000,
      totalPnl: 700,
      returnPct: 0.7,
      tradeCount: 4,
    })
  })

  it('returns no A-share tier summaries when no main or tier ledgers exist', async () => {
    const root = await createWorkspace()

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      signalQueueDir: join(root, 'signals'),
      now: new Date('2026-07-04T12:00:00.000Z'),
    })

    expect(snapshot.ashareTierSummaries).toBeUndefined()
  })
})
