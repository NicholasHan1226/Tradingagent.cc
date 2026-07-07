# TradingAgent 自动化任务清单

> 版本: 2026-06-29
> 归属仓库: TradingAgent (Tradingagent.cc)
> 总任务数: 36 条
> 调度方式: cron (待从 MarketGraph monolithic crontab 拆分)

---

## 任务详情

### 1. job_trading_signals — 全市场交易信号生成
- **目的**: 每 30 分钟消费 scores+events+regime 生成全市场交易信号卡
- **频率**: 每 30 分钟, 仅交易日 (*/30 * * * 1-5)
- **输入**: SharedSignals.backtest_cache (价格/技术), SharedSignals.events, MarketGraph.regime
- **输出**: signal_cards.jsonl (写入 shared/signals/)
- **失败处理**: 2 次重试 → 跳过本轮 → 下一轮合并未生成区间
- **归属仓库**: TradingAgent

### 2. job_premarket_signals — A 股盘前信号
- **目的**: 盘前 9:25 消费隔夜事件和评分生成当日开盘信号
- **频率**: 每日 09:25 (25 9 * * 1-5)
- **输入**: overnight_events, scores
- **输出**: premarket_signals.jsonl (写入 shared/signals/)
- **失败处理**: 3 次重试 → email alert 通知 Nicholas → 口头判断
- **归属仓库**: TradingAgent

### 3. job_ashare_sim_exec — A 股模拟执行
- **目的**: 盘中 5 分钟扫描条件触发, 在模拟账户执行买卖
- **频率**: 每 5 分钟, 盘中 9:30-15:00 (*/5 9-15 * * 1-5)
- **输入**: active_conditions, quotes (via SharedSignals)
- **输出**: sim_exec_log.jsonl (写入 shared/executions/sim/)
- **失败处理**: 3 次即时重试 → 跳过当前 tick → 下个 tick 继续
- **归属仓库**: TradingAgent

### 4. job_us_premarket — 美股盘前准备
- **目的**: 美股盘前消费美股日线和事件生成盘前信号
- **频率**: 每日 09:15 (15 9 * * 1-5)
- **输入**: US_daily, events
- **输出**: us_premarket_signals.jsonl (写入 shared/signals/us/)
- **失败处理**: 2 次重试 → 标记 degraded → 用 T-1 数据降级
- **归属仓库**: TradingAgent

### 5. job_us_hourly — 美股盘中信号
- **目的**: 美股交易时段每 30 分钟刷新信号
- **频率**: 每 30 分钟, 美股时段 (*/30 10-14,22-23,0-4 * * 1-5)
- **输入**: US_quotes, scores
- **输出**: us_intraday_signals.jsonl (写入 shared/signals/us/)
- **失败处理**: 2 次重试 → 跳过 → 下一时段补
- **归属仓库**: TradingAgent

### 6. job_us_shadow_exec — 美股影子执行
- **目的**: 美股时段影子盘执行 (模拟账户, 不涉及真实资金)
- **频率**: 每 30 分钟, 美股时段 (*/30 10-14,22-23,0-4 * * 1-5)
- **输入**: us_shadow_signals
- **输出**: us_shadow_trades.jsonl (写入 shared/executions/shadow/us/)
- **失败处理**: 2 次重试 → 记录 missed → 复盘标记
- **归属仓库**: TradingAgent

### 7. job_us_postclose — 美股收盘后处理
- **目的**: 美股收盘后汇总当日信号和执行结果
- **频率**: 每日 16:35 (35 16 * * 1-5)
- **输入**: US_close_data, 当日所有 US signals
- **输出**: us_postclose.jsonl (写入 shared/review/us/)
- **失败处理**: 2 次重试 → mark degraded → 次日 merge
- **归属仓库**: TradingAgent

### 8. job_crypto_shadow_exec — Crypto 影子执行
- **目的**: 24/7 影子盘执行 Crypto 交易训练
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: crypto_signals, klines
- **输出**: crypto_shadow_trades.jsonl (写入 shared/executions/shadow/crypto/)
- **失败处理**: 2 次重试 → 记录 missed → 复盘标记
- **归属仓库**: TradingAgent

### 9. job_crypto_daily — Crypto 日常信号
- **目的**: 每 4 小时生成 Crypto 信号
- **频率**: 每 4 小时 (0 */4 * * *)
- **输入**: crypto_klines, regime
- **输出**: crypto_daily_signals.jsonl (写入 shared/signals/crypto/)
- **失败处理**: 1 次重试 → 用 T-4h 缓存降级
- **归属仓库**: TradingAgent

### 10. job_crypto_weekly — Crypto 周常信号
- **目的**: 每 12 小时生成候选中期 Crypto 信号
- **频率**: 每 12 小时 (0 */12 * * *)
- **输入**: crypto_klines, events
- **输出**: crypto_weekly_signals.jsonl (写入 shared/signals/crypto/)
- **失败处理**: 1 次重试 → 用 T-12h 缓存降级
- **归属仓库**: TradingAgent

### 11. job_pm_shadow — Polymarket 影子执行
- **目的**: 影子盘模拟 Polymarket 预测市场交易
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: pm_prices, pm_markets
- **输出**: pm_shadow_trades.jsonl (写入 shared/executions/shadow/pm/)
- **失败处理**: 2 次重试 → 记录 missed → 复盘标记
- **归属仓库**: TradingAgent

### 12. job_pm_forward — PM 前向信号
- **目的**: 基于影子盘结果生成前向策略信号
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: pm_prices, pm_shadow
- **输出**: pm_forward_signals.jsonl (写入 shared/signals/pm/)
- **失败处理**: 2 次重试 → 跳过 → 下一轮合并
- **归属仓库**: TradingAgent

### 12b. job_pm_research_probability — PM 独立研究概率
- **目的**: 通过 MarketGraph 统一 API/read model 读取 PM 独立研究概率，并与 SharedSignals PM 市场/价格合并成 simulated edge 门禁文件；不写 SharedSignals，不写交易队列
- **频率**: 每 10 分钟错峰 (2-59/10 * * * *)
- **输入**: SharedSignals `/pm_markets` + `/pm_prices`、MarketGraph `GET /pm/research-probabilities` / `read_pm_research_probabilities`
- **输出**: model_probabilities.jsonl 与 model_probabilities_summary.json (写入 shared/review/pm/)
- **失败处理**: 3 次重试 → 无 MarketGraph 研究概率时原子清空旧概率文件，避免历史 edge 残留
- **归属仓库**: TradingAgent

### 13. job_pm_optimize — PM 策略优化
- **目的**: 小时级优化 PM 策略参数 (bayesian/weight adjustment)
- **频率**: 每小时 (0 * * * *)
- **输入**: pm_shadow, pm_forward
- **输出**: pm_optimize_params.json (写入 shared/strategies/pm/)
- **失败处理**: 2 次重试 → 保持上次参数不变 → 标记 stale
- **归属仓库**: TradingAgent

### 14. job_pm_promote — PM 信号晋级
- **目的**: PM 信号从影子晋级到前向执行层的升级评估
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: pm_signals, pm_review
- **输出**: pm_promotion.jsonl (写入 shared/review/pm/)
- **失败处理**: 2 次重试 → 保持当前级不变
- **归属仓库**: TradingAgent

### 15. job_auto_position — 自动仓位规划
- **目的**: 每小时基于资金账本和当前持仓规划仓位分配
- **频率**: 每小时 (45 * * * *)
- **输入**: capital_ledger, positions
- **输出**: position_plan.jsonl (写入 shared/accounting/)
- **失败处理**: 2 次重试 → 保持上次 plan → 标记 stale
- **归属仓库**: TradingAgent

### 16. job_pm_risk — PM 风控监控
- **目的**: 每 30 分钟检查 Polymarket 持仓风险 (VaR, exposure)
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: pm_positions, pm_prices
- **输出**: pm_risk_report.jsonl (写入 shared/risk/pm/)
- **失败处理**: 3 次重试 → email alert → 暂停 PM 新交易
- **归属仓库**: TradingAgent

### 17. job_stress_test — 全市场压力测试
- **目的**: 每周六凌晨对持仓执行历史场景压力测试
- **频率**: 每周六 03:00 (0 3 * * 6)
- **输入**: historical_data, positions
- **输出**: stress_test_report.json (写入 shared/risk/reports/)
- **失败处理**: 2 次重试 → 标记 skipped → 下周合并
- **归属仓库**: TradingAgent

### 18. gate_review (夜) — 夜间门禁审查
- **目的**: 凌晨 2:00 对当日信号-执行-仓位做门禁回溯裁决
- **频率**: 每日 02:00 (0 2 * * *)
- **输入**: signals, executions, positions
- **输出**: gate_decisions.jsonl (写入 shared/risk/gate/)
- **失败处理**: 2 次重试 → 推迟到 14:00 gate 合并
- **归属仓库**: TradingAgent

### 19. job_daily_brief (午) — 午间简报
- **目的**: 午盘后 (16:32 北京时间 = A 股午间收盘后) 生成半日回顾
- **频率**: 每日 16:32 (32 16 * * 1-5)
- **输入**: morning_trades
- **输出**: midday_review.jsonl (写入 shared/review/daily/)
- **失败处理**: 2 次重试 → 合并到晚间简报
- **归属仓库**: TradingAgent

### 20. job_daily_brief (晚) — 晚间简报
- **目的**: 晚间 22:00 全市场当日总结 (含美股盘中)
- **频率**: 每日 22:00 (0 22 * * 1-5)
- **输入**: full_day_trades
- **输出**: daily_brief.jsonl (写入 shared/review/daily/)
- **失败处理**: 2 次重试 → 次日晨补
- **归属仓库**: TradingAgent

### 21. gate_review (日) — 日间门禁审查
- **目的**: 每日 14:00 盘中信号门禁裁决
- **频率**: 每日 14:00 (0 14 * * *)
- **输入**: intraday_signals
- **输出**: gate_intraday.jsonl (写入 shared/risk/gate/)
- **失败处理**: 2 次重试 → 合并到夜间 gate
- **归属仓库**: TradingAgent

### 22. job_weekly_review — 全市场周复盘
- **目的**: 每周日晚 20:00 汇总周度交易复盘 (3 对比 + 归因 + 行动)
- **频率**: 每周日 20:00 (0 20 * * 0)
- **输入**: week_trades, benchmark
- **输出**: weekly_review.json (写入 shared/review/weekly/)
- **失败处理**: 2 次重试 → email alert → 手动执行
- **归属仓库**: TradingAgent

### 23. job_us_weekly — 美股周复盘
- **目的**: 每周日晚美股专项复盘
- **频率**: 每周日 20:30 (30 20 * * 0)
- **输入**: us_week_trades, us_benchmark
- **输出**: us_weekly_review.json (写入 shared/review/us/)
- **失败处理**: 2 次重试 → 标记 missed → 下周合并
- **归属仓库**: TradingAgent

### 24. job_us_signal_review — 美股信号复盘
- **目的**: 小时级复盘美股信号命中率和质量
- **频率**: 每小时, 仅交易日 (30 * * * 1-5)
- **输入**: us_signals
- **输出**: us_signal_review.jsonl (写入 shared/review/us/)
- **失败处理**: 2 次重试 → 跳过 → 下一小时合并
- **归属仓库**: TradingAgent

### 25. job_cross_market_review — 跨市场信号复盘
- **目的**: 小时级跨市场联动检验 (A 股事件对美股/Crypto 的影响兑现)
- **频率**: 每小时 (0 * * * *)
- **输入**: all_market_signals
- **输出**: cross_market_review.jsonl (写入 shared/review/cross/)
- **失败处理**: 2 次重试 → 跳过 → 下一小时合并
- **归属仓库**: TradingAgent

### 26. job_strategy_attribution — 策略归因
- **目的**: 按策略维度归因收益来源 (哪个策略赚/亏了多少)
- **频率**: 每小时 (30 * * * *)
- **输入**: strategy_trades
- **输出**: strategy_attribution.jsonl (写入 shared/review/attribution/)
- **失败处理**: 2 次重试 → 标记 degraded → 日终汇总时补
- **归属仓库**: TradingAgent

### 27. job_factor_attribution — 因子归因
- **目的**: 按因子维度归因收益 (六维: 宏观/事件/基本面/资金/技术/情绪)
- **频率**: 每小时 (30 * * * *)
- **输入**: factor_signals
- **输出**: factor_attribution.jsonl (写入 shared/review/attribution/)
- **失败处理**: 2 次重试 → 标记 degraded → 日终汇总时补
- **归属仓库**: TradingAgent

### 28. job_strategy_version — 策略版本快照
- **目的**: 小时级保存策略参数快照, 用于回溯和版本对比
- **频率**: 每小时 (0 * * * *)
- **输入**: strategy_params
- **输出**: strategy_version.jsonl (写入 shared/review/strategies/)
- **失败处理**: 2 次重试 → 跳过 → 下一小时补
- **归属仓库**: TradingAgent

### 29. job_backtest_report — 回测报告
- **目的**: 每日收盘后生成当日回测报告 (反事实: 如果执行了会怎样)
- **频率**: 每日 17:00 (0 17 * * 1-5)
- **输入**: backtest_results
- **输出**: backtest_report.json (写入 shared/review/backtest/)
- **失败处理**: 2 次重试 → 次日合并
- **归属仓库**: TradingAgent

### 30. job_research_report — 研究简报
- **目的**: 每日 16:00 生成研究发现的 markdown 简报
- **频率**: 每日 16:00 (0 16 * * 1-5)
- **输入**: research_findings
- **输出**: research_report.md (写入 shared/review/research/)
- **失败处理**: 2 次重试 → 跳过 → 次日合并
- **归属仓库**: TradingAgent

### 31. job_self_heal — 系统自愈循环
- **目的**: 每 15 分钟扫描系统日志和错误, 尝试自动修复已知问题
- **频率**: 每 15 分钟 (*/15 * * * *)
- **输入**: system_logs, errors
- **输出**: self_heal_actions.jsonl (写入 shared/review/heal/)
- **失败处理**: 3 次重试 → 记录无法自愈 → escalate to alert
- **归属仓库**: TradingAgent

### 32. self_heal (夜) — 夜间深度自愈
- **目的**: 凌晨 2:30 执行深度自愈 (数据修复、缓存清理、状态重置)
- **频率**: 每日 02:30 (30 2 * * *)
- **输入**: daily_errors, heal_state
- **输出**: heal_report.json (写入 shared/review/heal/)
- **失败处理**: 2 次重试 → email alert
- **归属仓库**: TradingAgent

### 33. daily_brief (晨) — 晨间简报
- **目的**: 早晨 7:30 汇总隔夜状态, 为当日交易做准备
- **频率**: 每日 07:30 (30 7 * * *)
- **输入**: overnight_state
- **输出**: morning_brief.json (写入 shared/review/daily/)
- **失败处理**: 2 次重试 → email alert → 手动查看
- **归属仓库**: TradingAgent

### 34. job_email_notify — 邮件通知汇总
- **目的**: 早晨 8:30 汇总所有复盘输出, 发送邮件给 Nicholas
- **频率**: 每日 08:30 (30 8 * * *)
- **输入**: all_review_outputs
- **输出**: emails_sent.jsonl (写入 shared/notify/logs/)
- **失败处理**: 3 次重试 → escalate to job_alert
- **归属仓库**: TradingAgent

### 35. job_alert — 告警通知
- **目的**: 每天 2 次检查系统健康 + 风险告警, 有异常发通知
- **频率**: 每日 08:00, 20:00 (0 8,20 * * *)
- **输入**: system_health, risk_alerts
- **输出**: alert_log.jsonl (写入 shared/notify/logs/)
- **失败处理**: 3 次重试 → 记录本地 → 下次告警时补偿发送
- **归属仓库**: TradingAgent

### 36. job_pm_report — PM 报告生成
- **目的**: 每 30 分钟生成 Polymarket 持仓和交易报告
- **频率**: 每 30 分钟 (*/30 * * * *)
- **输入**: pm_positions, pm_prices
- **输出**: pm_report.jsonl (写入 shared/notify/pm/)
- **失败处理**: 2 次重试 → 跳过 → 下一轮合并
- **归属仓库**: TradingAgent

---

## 汇总

| 类别 | 任务数 | 覆盖范围 |
|------|--------|----------|
| Trading Signals & Execution | 15 | 全市场/A/US/Crypto/PM |
| Risk | 3 | PM/全市场/Gate |
| Review & Attribution | 15 | 日/周/跨市场/策略/因子/回测/自愈 |
| Notify | 3 | 邮件/告警/PM报告 |
| **合计** | **36** | — |

## 调度方式

当前所有任务通过单一 `marketgraph` 用户 crontab 调度 (位于 `/opt/investment/MarketGraph/deploy/`)。
拆分后 tradingagent 任务将迁移至独立 crontab (`/opt/investment/tradingagent/shared/crontab.txt`),
统一通过 `env_loader.sh` 获取环境变量。

## 上游依赖

- **SharedSignals**: backtest_cache, tushare, events, marketdata_db, health_monitors
- **MarketGraph**: regime, event_impact, forward_calendar, scenario, causal_truth_table

## 下游消费

- **飞书/邮件**: Nicholas 的每日通知 (job_email_notify, job_alert)
- **MarketGraph 回传**: 执行价格 -> causal_truth_table (仅价格结果, 不传交易决策)
- **复盘台账**: 所有 review/attribution/heal 输出供策略迭代分析
