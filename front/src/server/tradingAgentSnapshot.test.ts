import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { describe, expect, it } from 'vitest'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot'

async function createWorkspace() {
  const root = join(tmpdir(), `tad-read-model-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  await mkdir(join(root, 'TradingAgent/shared/accounting'), { recursive: true })
  await mkdir(join(root, 'TradingAgent/shared/review/daily'), { recursive: true })
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
      }),
    )

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot: root,
      now: new Date('2026-07-04T10:00:00.000Z'),
    })

    expect(snapshot.holdings).toContainEqual(expect.objectContaining({ symbol: '600519.SH', market: 'A-share' }))
    expect(snapshot.signals).toContainEqual(expect.objectContaining({ symbol: '600519.SH', status: 'pending' }))
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
      { day: '7月3日', simulated: 2.4, target: 1.8, benchmark: 0.6, opportunity: -0.8 },
      { day: '7月4日', simulated: 3.1, target: 2, benchmark: 0.7, opportunity: -0.5 },
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
      { day: '7月3日', simulated: 1, target: 4, benchmark: 0, opportunity: -4 },
      { day: '现在', simulated: 1.73, target: 8, benchmark: 0, opportunity: -2 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 17.25,
      returnPct: 1.73,
      capitalBase: 1000,
      targetPct: 8,
      maxDrawdownPct: 4,
      tradeCount: 2,
      pointCount: 2,
      pnlSource: 'sim_ledger_mark_to_market',
      realizedPnl: 0.5,
      unrealizedPnl: 16.75,
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
      { day: '7月3日 09:00', simulated: 0.5, target: 2.67, benchmark: 0, opportunity: -1 },
      { day: '7月3日 10:00', simulated: 1, target: 5.33, benchmark: 0, opportunity: -1 },
      { day: '现在', simulated: 3, target: 8, benchmark: 0, opportunity: -1 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 30,
      returnPct: 3,
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
      { day: '7月4日 09:30', simulated: 0.8, target: 1.2, benchmark: 0.1, opportunity: -0.4 },
      { day: '现在', simulated: 2.5, target: 1.6, benchmark: 0.3, opportunity: -0.2 },
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
      { day: '7月3日 10:00', simulated: 0.2, target: 2.67, benchmark: 0, opportunity: 0 },
      { day: '7月4日 10:00', simulated: 0.5, target: 5.33, benchmark: 0, opportunity: 0 },
      { day: '现在', simulated: 0.7, target: 8, benchmark: 0, opportunity: 0 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 21,
      returnPct: 0.7,
      capitalBase: 3000,
      maxDrawdownPct: 1.4,
      tradeCount: 8,
      pointCount: 3,
      pnlSource: 'sim_ledger_mark_to_market',
      realizedPnl: 11,
      unrealizedPnl: 10,
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
      { day: '7月4日 10:00', simulated: 0.2, target: 2.67, benchmark: 0, opportunity: 0 },
      { day: '7月4日 10:06', simulated: 0.53, target: 5.33, benchmark: 0, opportunity: 0 },
      { day: '现在', simulated: 0.7, target: 8, benchmark: 0, opportunity: 0 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 21,
      returnPct: 0.7,
      capitalBase: 3000,
      tradeCount: 5,
    })
  })

  it('marks high plateau then rebase performance points as quality outliers', async () => {
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

    expect(snapshot.performance.map((point) => point.quality ?? 'normal')).toEqual([
      'normal',
      'outlier',
      'outlier',
      'outlier',
      'outlier',
      'outlier',
      'outlier',
      'normal',
    ])
    expect(snapshot.performance[1]).toMatchObject({
      qualityReason: '口径跳变候选',
      simulated: 90,
      target: 2,
    })
    expect(snapshot.performance.at(-1)).toMatchObject({
      day: '现在',
      simulated: 29.4,
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

  it('preserves signal stage timestamps so the funnel can animate the real decision path', async () => {
    const root = await createWorkspace()

    await writeFile(
      join(root, 'signals/pending/0700.HK.json'),
      JSON.stringify({
        ts_code: '0700.HK',
        market: 'HK',
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
      stage: '风控',
      status: '通过',
      source: 'signal_queue',
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      stage: '风控',
      status: '拦截',
      source: 'signal_queue',
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
      status: 'executed',
      method: 'Grid · 买入',
      stage: '成交',
      stageEvidence: 'replay',
      impact: '成交 $667',
    }))
    expect(snapshot.funnelEvents).toContainEqual(expect.objectContaining({
      symbol: 'BTC-USD',
      stage: '结果',
      status: '成交',
      source: 'sim_ledger',
    }))
    expect(snapshot.performance).toEqual([])
    expect(snapshot.domains.holdings.status).toBe('ready')
    expect(snapshot.domains.signals.status).toBe('ready')
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
})
