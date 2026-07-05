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

  it('aggregates latest same-day simulated ledger snapshots into one live portfolio result', async () => {
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
          trade_count: 2,
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
          trade_count: 3,
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
      { day: '7月3日 10:00', simulated: 0.2, target: 4, benchmark: 0, opportunity: 0 },
      { day: '现在', simulated: 0.5, target: 8, benchmark: 0, opportunity: 0 },
    ])
    expect(snapshot.portfolio).toMatchObject({
      pnlAmount: 15,
      returnPct: 0.5,
      capitalBase: 3000,
      maxDrawdownPct: 1.2,
      tradeCount: 5,
      pointCount: 2,
      pnlSource: 'sim_ledger_mark_to_market',
      realizedPnl: 7,
      unrealizedPnl: 8,
    })
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
})
