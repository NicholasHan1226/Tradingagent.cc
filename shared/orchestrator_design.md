# Tradings Orchestrator Design

## 1. 统一调度接口设计

### 1a. 调度阶段 (Phases)

Tradings 交易流程按时间轴分为 5 个阶段，每个阶段有独立触发条件和失败策略：

| 阶段 | 触发 | 窗口 | 失败策略 |
|------|------|------|----------|
| **pre_market** | 盘前 7:00-9:15 | 2h15m | 重试 2 次 → degrade 通知 |
| **intraday** | 盘中 9:30-15:00 | 5.5h | 间隔内重试 3 次 → 跳过本次 |
| **post_close** | 收盘 15:00-17:00 | 2h | 重试 2 次 → mark degraded |
| **overnight** | 夜间 17:00-次日 7:00 | 14h | 重试 1 次 → 次日 merge |
| **weekend** | 周六-周日 | 48h | 重试 1 次 → 下周期合并 |

### 1b. 频率分类 (Frequencies)

| 频率 | 标识 | 适用任务 |
|------|------|----------|
| 5min | `intraday_5min` | A 股模拟执行 |
| 15min | `every_15min` | 自愈循环 |
| 30min | `intraday_30min` / `every_30min` | 信号生成、影子执行、PM 任务 |
| hourly | `hourly` | 信号复盘、策略归因、因子归因 |
| 4x_daily | `4x_daily` | Crypto 日常 |
| 2x_daily | `2x_daily` | 告警通知、Crypto 周常 |
| daily | `daily_HH:MM` | 盘前信号、盘后简报、邮件通知 |
| weekly | `weekly_sun_HH:MM` / `weekly_sat_HH:MM` | 周复盘、压力测试 |

### 1c. 依赖声明 (Dependencies)

每个 cron 任务通过环境变量或配置文件声明输入依赖：

```
# 依赖声明格式 (在 wrapper 脚本中)
# @depends: SharedSignals.backtest_cache     (价格/技术指标)
# @depends: SharedSignals.tushare            (财务/资金流)
# @depends: SharedSignals.events             (事件/新闻)
# @depends: MarketGraph.regime               (经济象限)
# @depends: MarketGraph.event_impact         (事件→标的映射)
# @depends: MarketGraph.forward_calendar     (前瞻日历)
# @produces: Tradings.signals                (交易信号)
# @produces: Tradings.executions             (执行记录)
```

### 1d. 失败重试策略

```
Level 1: 立即重试 (interval 内)
  - 最多 3 次，指数退避 1s/5s/25s
  - 仅适用于网络瞬断、API 超时

Level 2: 时段内重试 (phase 内)
  - 时段结束前重试一次
  - 适用于上游数据未就绪

Level 3: 跨时段补偿 (next phase)
  - 记录 missed_signal 到 repair queue
  - 下一时段 merge 补偿执行

Level 4: 人工介入 (escalate)
  - 连续 3 次失败 → email alert
  - 策略级故障 → 飞书通知
```

---

## 2. Tradings Cron 任务清单 (36 条)

### 2a. Trading Signals & Execution (15 条)

| # | 任务 | Cron | 频率 | 市场 | 输入 | 输出 | 写入端 |
|---|------|------|------|------|------|------|--------|
| 1 | job_trading_signals | */30 * * * 1-5 | 30min | 全市场 | scores + events + regime | signal_cards.jsonl | signals/ |
| 2 | job_premarket_signals | 25 9 * * 1-5 | daily | Ashare | overnight_events + scores | premarket_signals.jsonl | signals/ |
| 3 | job_ashare_sim_exec | */5 9-15 * * 1-5 | 5min | Ashare | active_conditions + quotes | sim_exec_log.jsonl | executions/sim/ |
| 4 | job_us_premarket | 15 9 * * 1-5 | daily | US | US_daily + events | us_premarket_signals.jsonl | signals/us/ |
| 5 | job_us_hourly | */30 10-14,22-23,0-4 * * 1-5 | 30min | US | US_quotes + scores | us_intraday_signals.jsonl | signals/us/ |
| 6 | job_us_shadow_exec | */30 10-14,22-23,0-4 * * 1-5 | 30min | US | us_shadow_signals | us_shadow_trades.jsonl | executions/shadow/us/ |
| 7 | job_us_postclose | 35 16 * * 1-5 | daily | US | US_close_data | us_postclose.jsonl | review/us/ |
| 8 | job_crypto_shadow_exec | */30 * * * * | 30min | Crypto | crypto_signals + klines | crypto_shadow_trades.jsonl | executions/shadow/crypto/ |
| 9 | job_crypto_daily | 0 */4 * * * | 4x_daily | Crypto | crypto_klines + regime | crypto_daily_signals.jsonl | signals/crypto/ |
| 10 | job_crypto_weekly | 0 */12 * * * | 2x_daily | Crypto | crypto_klines + events | crypto_weekly_signals.jsonl | signals/crypto/ |
| 11 | job_pm_shadow | */30 * * * * | 30min | PM | pm_prices + pm_markets | pm_shadow_trades.jsonl | executions/shadow/pm/ |
| 12 | job_pm_forward | */30 * * * * | 30min | PM | pm_prices + pm_shadow | pm_forward_signals.jsonl | signals/pm/ |
| 13 | job_pm_optimize | 0 * * * * | hourly | PM | pm_shadow + pm_forward | pm_optimize_params.json | strategies/pm/ |
| 14 | job_pm_promote | */30 * * * * | 30min | PM | pm_signals + pm_review | pm_promotion.jsonl | review/pm/ |
| 15 | job_auto_position | 45 * * * * | hourly | 全市场 | capital_ledger + positions | position_plan.jsonl | accounting/ |

### 2b. Risk (3 条)

| # | 任务 | Cron | 频率 | 输入 | 输出 | 写入端 |
|---|------|------|------|------|------|--------|
| 16 | job_pm_risk | */30 * * * * | 30min | pm_positions + pm_prices | pm_risk_report.jsonl | risk/pm/ |
| 17 | job_stress_test | 0 3 * * 6 | weekly | historical_data + positions | stress_test_report.json | risk/reports/ |
| 18 | gate_review (夜) | 0 2 * * * | daily | signals + executions + positions | gate_decisions.jsonl | risk/gate/ |

### 2c. Review & Attribution (15 条)

| # | 任务 | Cron | 频率 | 输入 | 输出 | 写入端 |
|---|------|------|------|------|------|--------|
| 19 | job_daily_brief (午) | 32 16 * * 1-5 | daily | morning_trades | midday_review.jsonl | review/daily/ |
| 20 | job_daily_brief (晚) | 0 22 * * 1-5 | daily | full_day_trades | daily_brief.jsonl | review/daily/ |
| 21 | gate_review (日) | 0 14 * * * | daily | intraday_signals | gate_intraday.jsonl | risk/gate/ |
| 22 | job_weekly_review | 0 20 * * 0 | weekly | week_trades + benchmark | weekly_review.json | review/weekly/ |
| 23 | job_us_weekly | 30 20 * * 0 | weekly | us_week_trades + us_benchmark | us_weekly_review.json | review/us/ |
| 24 | job_us_signal_review | 30 * * * 1-5 | hourly | us_signals | us_signal_review.jsonl | review/us/ |
| 25 | job_cross_market_review | 0 * * * * | hourly | all_market_signals | cross_market_review.jsonl | review/cross/ |
| 26 | job_strategy_attribution | 30 * * * * | hourly | strategy_trades | strategy_attribution.jsonl | review/attribution/ |
| 27 | job_factor_attribution | 30 * * * * | hourly | factor_signals | factor_attribution.jsonl | review/attribution/ |
| 28 | job_strategy_version | 0 * * * * | hourly | strategy_params | strategy_version.jsonl | review/strategies/ |
| 29 | job_backtest_report | 0 17 * * 1-5 | daily | backtest_results | backtest_report.json | review/backtest/ |
| 30 | job_research_report | 0 16 * * 1-5 | daily | research_findings | research_report.md | review/research/ |
| 31 | job_self_heal | */15 * * * * | 15min | system_logs + errors | self_heal_actions.jsonl | review/heal/ |
| 32 | self_heal (夜) | 30 2 * * * | daily | daily_errors + heal_state | heal_report.json | review/heal/ |
| 33 | daily_brief (晨) | 30 7 * * * | daily | overnight_state | morning_brief.json | review/daily/ |

### 2d. Notify (3 条)

| # | 任务 | Cron | 频率 | 输入 | 输出 | 写入端 |
|---|------|------|------|------|------|--------|
| 34 | job_email_notify | 30 8 * * * | daily | all_review_outputs | emails_sent.jsonl | notify/logs/ |
| 35 | job_alert | 0 8,20 * * * | 2x_daily | system_health + risk_alerts | alert_log.jsonl | notify/logs/ |
| 36 | job_pm_report | */30 * * * * | 30min | pm_positions + pm_prices | pm_report.jsonl | notify/pm/ |

---

## 3. 与 SharedSignals 和 MarketGraph 的依赖关系

```
                        +----------------------+
                        |    SharedSignals     |
                        |  (数据采集层)         |
                        |                      |
                        |  backtest_cache      |-- 价格/技术指标/波动率
                        |  tushare (财务/资金)  |-- 基本面/资金流/北向
                        |  events (RSS/新闻)    |-- 事件/公告/宏观
                        |  marketdata_db       |-- 统一市场数据 SQLite
                        |  health_monitors     |-- 系统健康状态
                        +----------+-----------+
                                   | read-only
                                   v
                 +-------------------------------+
                 |         MarketGraph           |
                 |    (跨市场联动图谱)            |
                 |                               |
                 |  regime (Dalio 4象限)          |-- 经济季节+倾斜
                 |  event_impact (因果链)         |-- 事件->标的映射
                 |  forward_calendar              |-- 前瞻事件日历
                 |  scenario (情景分析)           |-- 宏观情景
                 |  causal_truth_table            |-- 因果兑现率
                 +-----------+-------------------+
                             | read-only
                             v
        +--------------------------------------------+
        |              Tradings                      |
        |         (交易决策+执行+复盘)                |
        |                                            |
        |  +- screening -- 消费 scores + events      |
        |  +- adversarial - 消费 events + scenarios  |
        |  +- risk -------- 消费 regime + calendar   |
        |  +- portfolio -- 消费 volatility + regime  |
        |  +- execution -- 消费 quotes (via Shared)  |
        |  +- review ----- 消费 benchmark + truth    |
        |  +- accounting - 自包含 (本地账本)         |
        |  +- notify ----- 消费 review_outputs       |
        |                                            |
        |  产出 -> MarketGraph 回传 (仅价格结果):     |
        |  - 执行价格 -> causal_truth_table          |
        |  - 策略收益 -> 因果验证                     |
        +--------------------------------------------+
```

### 3a. 依赖矩阵

| Tradings 模块 | SharedSignals 依赖 | MarketGraph 依赖 |
|---------------|-------------------|-----------------|
| screening | backtest_cache, tushare daily/finance, events | regime, event_impact |
| adversarial | events (raw) | scenario, causal_truth |
| risk | backtest_cache, margin_detail | regime, forward_calendar |
| portfolio | backtest_cache (volatility) | regime, all_weather |
| execution | backtest_cache (quotes) | -- (仅消费价格) |
| review | benchmark data | event_impact, truth_table |
| accounting | -- (自包含) | -- |
| notify | -- | -- |

### 3b. Fail-Safe 策略

- SharedSignals 不可用时: degrade 评分权重 -> 使用缓存最近值 -> 标记 `data_freshness: stale`
- MarketGraph 不可用时: regime 使用 T-1 值 -> event_impact 跳过 -> 标记 `regime: unknown`
- 两个都不可用时: 只执行风控监控 + 已有仓位退出逻辑 -> 不开新仓
