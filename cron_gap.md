# Cron 迁移差距分析报告

> **⚠️ 本文件是 2026-06-29 的一次性快照。** 运行时状态（cron 任务数、gap 结论）可能已过时。当前权威状态以 [STATUS.md](STATUS.md) 和服务器 crontab 为准。

> 生成时间: 2026-06-29
> 分析员: cron 迁移校验员 (只读)
> 基准文件:
> - `shared/automation_tasks.md` — 36 任务计划
> - `shared/orchestrator_design.md` — 调度设计
> - `shared/cron_inventory.csv` — 101 旧 cron 清单
> - `crontab_remote.txt` — 实际 crontab (103 行, 全部跑 `/opt/investment/MarketGraph/deploy/`)

---

## 1. 36 任务计划 vs 实际 crontab 映射

### 1.1 全覆盖结论: 36/36 全部映射, 无缺口

所有 36 个计划任务在实际 crontab 中均有对应条目, **无新增任务、无缺失条目**。

### 1.2 逐任务映射表

| # | 计划任务名 | 计划 Cron | 实际 Crontab 行号 | 实际执行路径 | Cron 一致? |
|---|-----------|-----------|-------------------|-------------|-----------|
| 1 | job_trading_signals | `*/30 * * * 1-5` | 69 | `/opt/.../deploy/job_trading_signals.sh` | ✓ |
| 2 | job_premarket_signals | `25 9 * * 1-5` | 86 | `/opt/.../deploy/job_premarket_signals.sh` | ✓ |
| 3 | job_ashare_sim_exec | `*/5 9-15 * * 1-5` | 93 | `/opt/.../deploy/wrappers/job_ashare_sim_exec.sh` | ✓ |
| 4 | job_us_premarket | `15 9 * * 1-5` | 58 | `/opt/.../deploy/wrappers/job_us_premarket.sh` | ✓ |
| 5 | job_us_hourly | `*/30 10-14,22-23,0-4 * * 1-5` | 59 | `/opt/.../deploy/wrappers/job_us_hourly.sh` | ✓ |
| 6 | job_us_shadow_exec | `*/30 10-14,22-23,0-4 * * 1-5` | 84 | `/opt/.../deploy/wrappers/job_us_shadow_exec.sh` | ✓ |
| 7 | job_us_postclose | `35 16 * * 1-5` | 60 | `/opt/.../deploy/wrappers/job_us_postclose.sh` | ✓ |
| 8 | job_crypto_shadow_exec | `*/30 * * * *` | 85 | `/opt/.../deploy/wrappers/job_crypto_shadow_exec.sh` | ✓ |
| 9 | job_crypto_daily | `0 */4 * * *` | 62 | `/opt/.../deploy/wrappers/job_crypto_daily.sh` | ✓ |
| 10 | job_crypto_weekly | `0 */12 * * *` | 63 | `/opt/.../deploy/wrappers/job_crypto_weekly.sh` | ✓ |
| 11 | job_pm_shadow | `*/30 * * * *` | 53 | `/opt/.../deploy/wrappers/job_pm_shadow.sh` | ✓ |
| 12 | job_pm_forward | `*/30 * * * *` | 54 | `/opt/.../deploy/wrappers/job_pm_forward.sh` | ✓ |
| 13 | job_pm_optimize | `0 * * * *` | 55 | `/opt/.../deploy/wrappers/job_pm_optimize.sh` | ✓ |
| 14 | job_pm_promote | `*/30 * * * *` | 94 | `/opt/.../deploy/job_pm_promote.sh` | ✓ |
| 15 | job_auto_position | `45 * * * *` | 67 | `/opt/.../deploy/wrappers/job_auto_position.sh` | ✓ |
| 16 | job_pm_risk | `*/30 * * * *` | 56 | `/opt/.../deploy/wrappers/job_pm_risk.sh` | ✓ |
| 17 | job_stress_test | `0 3 * * 6` | 91 | `/opt/.../deploy/job_stress_test.sh` | ✓ |
| 18 | gate_review (夜) | `0 2 * * *` | 80 | `venv/bin/python3 .../gate_review.py --apply --json` | ✓ |
| 19 | job_daily_brief (午) | `32 16 * * 1-5` | 34 | `/opt/.../deploy/job_daily_brief.sh` | ✓ |
| 20 | job_daily_brief (晚) | `0 22 * * 1-5` | 5 | `/opt/.../deploy/job_daily_brief.sh` | ✓ |
| 21 | gate_review (日) | `0 14 * * *` | 95 | `venv/bin/python3 .../gate_review.py --apply --json` | ✓ |
| 22 | job_weekly_review | `0 20 * * 0` | 3 | `/opt/.../deploy/job_weekly_review.sh` | ✓ |
| 23 | job_us_weekly | `30 20 * * 0` | 61 | `/opt/.../deploy/wrappers/job_us_weekly.sh` | ✓ |
| 24 | job_us_signal_review | `30 * * * 1-5` | 64 | `/opt/.../deploy/wrappers/job_us_signal_review.sh` | ✓ |
| 25 | job_cross_market_review | `0 * * * *` | 65 | `/opt/.../deploy/wrappers/job_cross_market_review.sh` | ✓ |
| 26 | job_strategy_attribution | `30 * * * *` | 66 | `/opt/.../deploy/wrappers/job_strategy_attribution.sh` | ✓ |
| 27 | job_factor_attribution | `30 * * * *` | 89 | `/opt/.../deploy/wrappers/job_factor_attribution.sh` | ✓ |
| 28 | job_strategy_version | `0 * * * *` | 70 | `/opt/.../deploy/wrappers/job_strategy_version.sh` | ✓ |
| 29 | job_backtest_report | `0 17 * * 1-5` | 90 | `/opt/.../deploy/wrappers/job_backtest_report.sh` | ✓ |
| 30 | job_research_report | `0 16 * * 1-5` | 92 | `/opt/.../deploy/job_research_report.sh` | ✓ |
| 31 | job_self_heal | `*/15 * * * *` | 87 | `/opt/.../deploy/job_self_heal.sh` | ✓ |
| 32 | self_heal (夜) | `30 2 * * *` | 96 | `venv/bin/python3 .../self_heal.py --apply --json` | ✓ |
| 33 | daily_brief (晨) | `30 7 * * *` | 88 | `venv/bin/python3 .../daily_brief.py --apply --json` | ✓ |
| 34 | job_email_notify | `30 8 * * *` | 68 | `/opt/.../deploy/job_email_notify.sh` | ✓ |
| 35 | job_alert | `0 8,20 * * *` | 122 | `bash /opt/.../deploy/job_alert.sh` | ✓ |
| 36 | job_pm_report | `*/30 * * * *` | 57 | `/opt/.../deploy/wrappers/job_pm_report.sh` | ✓ |

### 1.3 映射方式分类

| 调用类型 | 数量 | 涉及任务 # |
|---------|------|-----------|
| `deploy/wrappers/job_*.sh` (wrapper 脚本) | 23 | 3-13,15,16,23-29,36 |
| `deploy/job_*.sh` (直接 shell) | 10 | 1,2,14,17,19,20,22,30,31,34 |
| `venv/bin/python3 .../tools/{script}.py --apply --json` (直接 Python) | 2 | 18/21(gate_review), 32(self_heal 夜), 33(daily_brief) |
| `bash job_alert.sh` | 1 | 35 |

**重要发现**: 任务 #18(gate_review 夜)、#21(gate_review 日)、#32(self_heal 夜)、#33(daily_brief 晨) 4 个任务当前使用**直接 Python 调用**而非 wrapper 脚本。迁移到 tradingagent 后需要为这些任务创建对应的 wrapper 脚本。

---

## 2. 未覆盖的旧 cron (66 条)

### 2.1 实际 crontab 总览

实际 crontab 共 **102 条可执行 cron** (排除注释行和 `BASH_ENV=`/`SHELL=` 环境变量行):
- **36 条** → 映射到 tradingagent 36 任务计划
- **66 条** → 36 任务计划外, 需归属到 MarketGraph / SharedSignals / 停删

### 2.2 归属 MarketGraph (保留) — 25 条

|crontab 行| Cron | 脚本 | 模块 |
|----------|------|------|------|
| 4 | `0 21 * * 0` | job_weekly_calibrate.sh | 周度校准 |
| 14 | `0 8 * * 1` | job_seasonal_context.sh | 季节性上下文 |
| 15 | `0 9,17 * * 1-5` | agent_announcements.sh | 公告代理 |
| 16 | `10,25,40,55 * * * *` | agent_repair.sh | 修复代理 (15min) |
| 17 | `10 2,8,14,18 * * *` | agent_macro.sh | 宏观代理 (4x/d) |
| 19 | `*/10 * * * *` | job_runtime_checkpoint.sh | 运行时检查点 |
| 21 | `15 9 * * 1-5` | job_regime.sh | 经济象限刷新 |
| 22 | `*/15 * * * *` | agent_radar.sh | 雷达代理 |
| 23 | `*/15 * * * *` | promote_chain.sh | 晋升链 |
| 24 | `*/15 * * * *` | repair_pack.sh | 修复包 |
| 27 | `17 * * * *` | agent_reaction.sh | 反应代理 |
| 28 | `20 16 * * 1-5` | agent_attribution.sh | 归因代理 |
| 29 | `*/2 9-15 * * 1-5` | agent_technical.sh | 技术面代理 (2min) |
| 32 | `30 8,20 * * *` | agent_ushk.sh | US/HK 代理 |
| 33 | `*/30 9-15 * * 1-5` | agent_market_depth.sh | 市场深度代理 |
| 36 | `45 20 * * 1-5` | postclose_review.sh | 收盘后复盘 |
| 37 | `3,18,33,48 * * * *` | agent_concept.sh | 概念股代理 (15min) |
| 38 | `9,24,39,54 * * * *` | agent_enterprise.sh | 企业代理 (15min) |
| 39 | `*/5 * * * *` | job_sentiment_fast_promote.sh | 情绪快速晋升 (5min) |
| 40 | `6,21,36,51 * * * *` | agent_chain.sh | 链代理 (15min) |
| 71 | `*/5 * * * *` | `_dashboard.py` | 仪表盘刷新 |
| 73 | `30 * * * *` | `_auto_tune.py` | 自动调参 |
| 74 | `@reboot` | `_api_server.py &` | API 服务器 |
| 120 | `*/5 * * * *` | `marketgraph_signal_to_strategy.py` | 信号→策略桥 |
| 124 | `30 21 * * 0` | `weight_adjuster.py --backtest-ok` | 权重调整器 |

### 2.3 归属 SharedSignals (移动) — 40 条

| crontab 行| Cron | 脚本 | 模块 |
|----------|------|------|------|
| 2 | `0 13 * * 0` | job_collect_enterprise.sh | 企业收集(周日) |
| 6 | `0,30 9-15 * * 1-5` | job_news.sh | 新闻收集(30min) |
| 7 | `0 3 * * 0` | job_cache_warm_weekly.sh | 缓存预热(周) |
| 8 | `0 3 * * *` | job_collect_enterprise.sh | 企业收集(日) |
| 9 | `0 4 * * 0` | job_archive_cleanup.sh | 归档清理 |
| 10 | `0 4 * * 4` | job_collect_financials.sh | 财报收集 |
| 11 | `0 5 * * 1-5` | job_collect_news.sh | 新闻收集(日) |
| 12 | `0 7,19 * * *` | job_health_sweep.sh | 健康扫描 |
| 13 | `0 8,18 * * *` | job_collect_macro.sh | 宏观数据 |
| 18 | `*/10 9-15 * * 1-5` | job_cache_warm_intraday.sh | 缓存预热(盘中) |
| 20 | `15 16 * * 1-5` | job_cache_warm.sh | 缓存预热 |
| 25 | `16 20 * * 1-5` | job_collect_cross_market.sh | 跨市场收集 |
| 26 | `17 16 * * 1-5` | job_cache_warm_extended.sh | 缓存预热(扩展) |
| 30 | `30 20 * * 0` | refresh_trade_calendar.py | 交易日历刷新 |
| 31 | `30 8,12,16 * * *` | job_fx_rates.sh | 汇率 (3x/d) |
| 35 | `40 23 * * *` | job_derived_refresh.sh | 派生指标刷新 |
| 41 | `* 9-15 * * 1-5` | job_intraday_snapshot_loop.sh | 盘中快照循环 |
| 44 | `@reboot` | job_rss_collector.sh | RSS 采集器 |
| 72 | `0 6 * * *` | `_external_sources.py` | 外部数据源 |
| 75 | `@reboot` | start_rsshub.sh | RSSHub 启动 |
| 77 | `*/10 * * * *` | collector.py --tier hot | RSS 热层 (10min) |
| 78 | `5 * * * *` | collector.py --tier warm | RSS 温层 (hourly) |
| 82 | `0 1,13 * * *` | source_auto_graduate.py | 信源自动毕业 |
| 83 | `0 3 * * *` | signal_log_cleanup.py | 信号日志清理 |
| 98 | `0 */2 * * *` | job_data_capability_registry.sh | 数据能力注册表 |
| 100 | `*/5 9-15 * * 1-5` | job_tushare_rt_min_5m.sh | Tushare 实时分钟 |
| 102 | `35 6 * * 1-5` | tushare_dimension_collect --research | Tushare 研报 |
| 103 | `25,35 9 * * 1-5` | tushare_dimension_collect --auction | Tushare 集合竞价 |
| 104 | `25 16 * * 1-5` | tushare_dimension_collect --stock_minutes | Tushare 分钟线 |
| 106 | `*/5 * * * *` | job_marketdata_db_sync.sh | 统一数据库同步 |
| 107 | `45 16 * * 1-5` | job_ashare_daily_backfill_db.sh --max-days 1 | A股日线回填 |
| 108 | `10 */2 * * *` | job_ashare_daily_backfill_db.sh --resume | A股历史回溯 |
| 110 | `*/10 9-16 * * 1-5` | tushare_dimension_collect --events | Tushare 事件(盘中) |
| 111 | `20 6,12,17,21 * * *` | tushare_dimension_collect --events | Tushare 事件(4x/d) |
| 113 | `*/15 * * * *` | job_pm_collect.sh | PM 数据采集 |
| 114 | `*/15 * * * *` | job_collect_crypto.sh | Crypto 数据采集 |
| 115 | `*/15 * * * *` | job_crypto_hourly.sh | Crypto 小时级收集 |
| 116 | `25 2 * * *` | job_marketdata_db_maintenance.sh | DB 维护 |
| 118 | `20 */2 * * 1-6` | tushare_dimension_collect --us_daily | Tushare 美股日线 |
| 121 | `*/10 * * * *` | job_health_monitor.sh | 健康监控 |
| 123 | `0 4 * * *` | auto_maintain.sh | 自动维护 |

### 2.4 实际 crontab 有但 cron_inventory.csv 未收录的条目 — 1 条

| crontab 行 | Cron | 脚本 | 说明 |
|-----------|------|------|------|
| 124 | `30 21 * * 0` | `weight_adjuster.py --backtest-ok` | 权重调整器, 未在 101 旧清单中 |

**归属建议**: MarketGraph (分析层, 权重/策略相关, 非 tradingagent 复盘范围)

---

## 3. 频率合理性评估

### 3.1 36 任务频率分布

| 频率级别 | 任务数 | 典型任务 | 日运行次数 |
|---------|--------|---------|-----------|
| 5min | 1 | #3 A股模拟执行 (仅盘中) | ~66/日 |
| 15min | 1 | #31 自愈循环 | 96/日 (24×7) |
| 30min | 10 | #1,#5,#6,#8,#11,#12,#14,#16,#36,#? | 32-48/日 |
| hourly | 7 | #13,#24,#25,#26,#27,#28,#? | 24/日 |
| 4h | 1 | #9 Crypto日常 | 6/日 |
| 12h | 1 | #10 Crypto周常 | 2/日 |
| daily | 12 | #2,#4,#7,#18,#19,#20,#21,#29,#30,#32,#33,#34 | 1/日 |
| 2x/daily | 1 | #35 告警 | 2/日 |
| weekly | 2 | #17,#22,#23 | 1/周 |

### 3.2 潜在频率问题

#### 3.2.1 30分钟集群过密 (10 个任务)

以下任务全部 `*/30`, 且 24/7 运行:
- #8 crypto_shadow_exec (48x/日)
- #11 pm_shadow (48x/日)
- #12 pm_forward (48x/日)
- #14 pm_promote (48x/日)
- #16 pm_risk (48x/日)
- #36 pm_report (48x/日)

6 个 PM/Crypto 任务在 `*/30` 整点同时触发, 瞬时负载峰值。**建议**: 错开 2-3 分钟, 例如 `*/30` → 改为 `0,30 / 3,33 / 6,36` 分散。

#### 3.2.2 self_heal 15分钟 24/7 偏高

`#31 job_self_heal` (*/15) 每天 96 次, 包含周末凌晨。对比 MarketGraph 的 `agent_repair` 也是 `*/15`. **建议**: 区分日/夜间频率 — 盘中 `*/15`, 夜间 (22:00-07:00) 降为 `*/60`。

#### 3.2.3 US 任务 cron 复杂度风险

`#5 job_us_hourly` 和 `#6 job_us_shadow_exec` 使用:
```
*/30 10-14,22-23,0-4 * * 1-5
```
这个表达式覆盖: 周一至周五, 小时=10,11,12,13,14 + 22,23,0,1,2,3,4, 每 30min.
实际对应: 北京时间 10:00-14:59 (美股开盘前+早盘) + 22:00-04:59 (美股主要交易时段).

**正确性**: 表达式正确, 但可维护性差。建议改为多行 cron 或用 wrapper 内判断。

#### 3.2.4 与旧 MarketGraph 代理的频率比较

| MarketGraph agent | 频率 | 对比 tradingagent |
|-------------------|------|-------------|
| agent_repair | */15 | = self_heal (*/15) |
| agent_radar | */15 | 无直接对应 |
| agent_technical | */2 | A股执行 (*/5) — tradingagent 更稀 |
| agent_market_depth | */30 (仅盘中) | trading_signals */30 (仅交易日) — 一致 |
| agent_reaction | hourly | PM optimize / cross_market_review — 一致 |
| marketgraph_signal_to_strategy | */5 | 无直接对应 |

总体: tradingagent 频率设计**合理**, 没有明显过频/过稀的问题。`*/30` PM 集群是唯一需要关注的优化点。

---

## 4. 三仓库归属拆分方案

### 4.1 总体拆分

| 仓库 | 实际 crontab 条目 | 占比 |
|------|------------------|------|
| tradingagent (迁移) | 36 | 35% |
| MarketGraph (保留) | 25 | 25% |
| SharedSignals (移动) | 40 | 39% |
| 未定 (weight_adjuster) | 1 | 1% |
| **合计** | **102** | 100% |

### 4.2 拆分后各仓库职责

| 仓库 | 职责 | 脚本路径前缀 | 独立 crontab |
|------|------|-------------|-------------|
| **tradingagent** | 信号生成、模拟执行、风控、复盘、通知 | `/opt/investment/tradingagent/` | `marketgraph` 用户新增独立 crontab |
| **MarketGraph** | 跨市场图谱、agent 代理、regime/calendar、dashboard/API | `/opt/investment/MarketGraph/deploy/` | `marketgraph` 用户 crontab (精简后) |
| **SharedSignals** | 数据采集 (tushare/RSS/crypto/PM)、缓存预热、DB 同步/维护、健康监控 | `/opt/investment/SharedSignals/` | `marketgraph` 用户 crontab (共享层) 或独立用户 |

### 4.3 归属边界案例

- `job_weekly_review.sh` (#22): Cron inventory 标 `move`, 属于 tradingagent 复盘层 → **tradingagent**
- `job_daily_brief.sh` (#19/#20): Cron inventory 标 `move`, 同一脚本双 cron → **tradingagent** (需拆为两个独立脚本或 wrapper 参数化)
- `gate_review.py` (#18/#21): 当前跑在 MarketGraph 工具目录, 但逻辑是 tradingagent 门禁审查 → **tradingagent** (需创建 wrapper)
- `self_heal.py` (#32): 同上 → **tradingagent**
- `daily_brief.py` (#33): 同上 → **tradingagent**
- `weight_adjuster.py` (行 124): 未在旧清单, 权重策略相关 → **MarketGraph**
- `marketgraph_signal_to_strategy.py` (行 120): Signal→Strategy 桥 → **MarketGraph**
- `_dashboard.py`, `_api_server.py`, `_auto_tune.py`: 基础设施 → **MarketGraph**

---

## 5. 迁移与停删清单

### 5.1 迁移到 tradingagent 的 cron (36 条)

迁移到独立 crontab, 路径从 `/opt/investment/MarketGraph/deploy/` → `/opt/investment/tradingagent/shared/` (wrapper 目录) 或 `/opt/investment/tradingagent/{module}/`.

需要通过 `env_loader.sh` 获取环境变量, 不再依赖 `BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh`.

#### 5.1.1 需新建 wrapper 脚本的任务 (4 个)

当前直接用 `python3` 调用, 迁移后需 wrapper:

| # | 任务 | 当前实际调用 | 建议 tradingagent wrapper |
|---|------|------------|---------------------|
| 18/21 | gate_review (夜/日) | `venv/bin/python3 /opt/.../tools/gate_review.py --apply --json` | `tradingagent/shared/wrappers/gate_review_night.sh` / `gate_review_intraday.sh` |
| 32 | self_heal (夜) | `venv/bin/python3 /opt/.../tools/self_heal.py --apply --json` | `tradingagent/shared/wrappers/job_self_heal_deep.sh` |
| 33 | daily_brief (晨) | `venv/bin/python3 /opt/.../tools/daily_brief.py --apply --json` | `tradingagent/shared/wrappers/job_daily_brief_morning.sh` |

Wrapper 模板结构:
```bash
#!/bin/bash
source /opt/investment/tradingagent/shared/env_loader.sh
cd /opt/investment/tradingagent
python3 -m tradingagent.{module} --apply --json >> /opt/investment/tradingagentRuntime/logs/{task}.log 2>&1
```

#### 5.1.2 需拆分为独立脚本的任务 (1 个)

`job_daily_brief.sh` (#19 午 + #20 晚) 同一脚本两次调用, 迁移后建议:
- `tradingagent/shared/wrappers/job_daily_brief_midday.sh` — #19 午间简报 (32 16 * * 1-5)
- `tradingagent/shared/wrappers/job_daily_brief_evening.sh` — #20 晚间简报 (0 22 * * 1-5)

#### 5.1.3 迁移后 tradingagent 独立 crontab 示例

```cron
# tradingagent crontab — 独立于 MarketGraph
# 环境加载
TRADINGAGENT_ENV=/opt/investment/tradingagent/shared/env_loader.sh

# Trading Signals & Execution (15)
*/30 * * * 1-5              source $TRADINGAGENT_ENV && /opt/investment/tradingagent/shared/wrappers/job_trading_signals.sh
25 9 * * 1-5                source $TRADINGAGENT_ENV && /opt/investment/tradingagent/shared/wrappers/job_premarket_signals.sh
*/5 9-15 * * 1-5            source $TRADINGAGENT_ENV && /opt/investment/tradingagent/Ashare/wrappers/job_ashare_sim_exec.sh
15 9 * * 1-5                source $TRADINGAGENT_ENV && /opt/investment/tradingagent/US/wrappers/job_us_premarket.sh
*/30 10-14,22-23,0-4 * * 1-5 source $TRADINGAGENT_ENV && /opt/investment/tradingagent/US/wrappers/job_us_hourly.sh
# ... (其余 30 条)
```

### 5.2 保留在 MarketGraph 的 cron (25 条)

保持不变, 继续运行在 `/opt/investment/MarketGraph/deploy/` 下:

| 序号 | 脚本 | 说明 |
|------|------|------|
| 1 | job_weekly_calibrate.sh | 周度校准, MarketGraph 核心 |
| 2 | job_seasonal_context.sh | 季节性上下文 |
| 3 | mg_agent/agent_announcements.sh | 公告代理 |
| 4 | mg_agent/agent_repair.sh | 修复代理 |
| 5 | mg_agent/agent_macro.sh | 宏观代理 |
| 6 | job_runtime_checkpoint.sh | 运行时检查点 |
| 7 | job_regime.sh | 经济象限 |
| 8 | mg_agent/agent_radar.sh | 雷达代理 |
| 9 | mg_agent/promote_chain.sh | 晋升链 |
| 10 | mg_agent/repair_pack.sh | 修复包 |
| 11 | mg_agent/agent_reaction.sh | 反应代理 |
| 12 | mg_agent/agent_attribution.sh | 归因代理 |
| 13 | mg_agent/agent_technical.sh | 技术面代理 |
| 14 | mg_agent/agent_ushk.sh | US/HK 代理 |
| 15 | mg_agent/agent_market_depth.sh | 市场深度代理 |
| 16 | mg_agent/postclose_review.sh | 收盘后复盘 |
| 17 | mg_agent/agent_concept.sh | 概念股代理 |
| 18 | mg_agent/agent_enterprise.sh | 企业代理 |
| 19 | job_sentiment_fast_promote.sh | 情绪快速晋升 |
| 20 | mg_agent/agent_chain.sh | 链代理 |
| 21 | `_dashboard.py` | 仪表盘 |
| 22 | `_auto_tune.py` | 自动调参 |
| 23 | `_api_server.py` | API 服务器 |
| 24 | `marketgraph_signal_to_strategy.py` | 信号→策略桥 |
| 25 | `weight_adjuster.py --backtest-ok` | 权重调整器 |

### 5.3 归属 SharedSignals 的 cron (40 条)

迁移到 `/opt/investment/SharedSignals/` 目录, 从 MarketGraph deploy 目录剥离:

关键组:
- **Tushare 采集**: 7 条 (行 100/102/103/104/107/108/110/111/118)
- **RSS 采集**: 4 条 (行 44/75/77/78)
- **缓存/预热**: 6 条 (行 7/18/20/25/26/116)
- **企业数据**: 3 条 (行 2/8/10)
- **新闻/宏观**: 4 条 (行 6/11/13/31)
- **健康监控**: 3 条 (行 12/121/123)
- **Crypto/PM 采集**: 3 条 (行 113/114/115)
- **DB维护**: 3 条 (行 83/98/106)
- **其他**: 7 条 (行 9/30/35/41/72/82/98)

### 5.4 迁移后应停删的旧 cron

在 tradingagent 独立 crontab 激活后, 应从 MarketGraph crontab 中**删除**以下 36 条:

```
#19 (午)  32 16 * * 1-5    /opt/.../deploy/job_daily_brief.sh
#20 (晚)  0 22 * * 1-5     /opt/.../deploy/job_daily_brief.sh
#22       0 20 * * 0       /opt/.../deploy/job_weekly_review.sh
#1         */30 * * * 1-5  /opt/.../deploy/job_trading_signals.sh
#2         25 9 * * 1-5    /opt/.../deploy/job_premarket_signals.sh
#3         */5 9-15 * * 1-5 /opt/.../deploy/wrappers/job_ashare_sim_exec.sh
#4         15 9 * * 1-5    /opt/.../deploy/wrappers/job_us_premarket.sh
#5         */30 ...         /opt/.../deploy/wrappers/job_us_hourly.sh
#6         */30 ...         /opt/.../deploy/wrappers/job_us_shadow_exec.sh
#7         35 16 * * 1-5   /opt/.../deploy/wrappers/job_us_postclose.sh
#8         */30 * * * *    /opt/.../deploy/wrappers/job_crypto_shadow_exec.sh
#9         0 */4 * * *     /opt/.../deploy/wrappers/job_crypto_daily.sh
#10        0 */12 * * *    /opt/.../deploy/wrappers/job_crypto_weekly.sh
#11        */30 * * * *    /opt/.../deploy/wrappers/job_pm_shadow.sh
#12        */30 * * * *    /opt/.../deploy/wrappers/job_pm_forward.sh
#13        0 * * * *       /opt/.../deploy/wrappers/job_pm_optimize.sh
#14        */30 * * * *    /opt/.../deploy/job_pm_promote.sh
#15        45 * * * *      /opt/.../deploy/wrappers/job_auto_position.sh
#16        */30 * * * *    /opt/.../deploy/wrappers/job_pm_risk.sh
#17        0 3 * * 6       /opt/.../deploy/job_stress_test.sh
#18(夜)   0 2 * * *       .../gate_review.py --apply --json
#21(日)   0 14 * * *      .../gate_review.py --apply --json
#23        30 20 * * 0     /opt/.../deploy/wrappers/job_us_weekly.sh
#24        30 * * * 1-5    /opt/.../deploy/wrappers/job_us_signal_review.sh
#25        0 * * * *       /opt/.../deploy/wrappers/job_cross_market_review.sh
#26        30 * * * *      /opt/.../deploy/wrappers/job_strategy_attribution.sh
#27        30 * * * *      /opt/.../deploy/wrappers/job_factor_attribution.sh
#28        0 * * * *       /opt/.../deploy/wrappers/job_strategy_version.sh
#29        0 17 * * 1-5    /opt/.../deploy/wrappers/job_backtest_report.sh
#30        0 16 * * 1-5    /opt/.../deploy/job_research_report.sh
#31        */15 * * * *    /opt/.../deploy/job_self_heal.sh
#32(夜)   30 2 * * *      .../self_heal.py --apply --json
#33(晨)   30 7 * * *      .../daily_brief.py --apply --json
#34        30 8 * * *      /opt/.../deploy/job_email_notify.sh
#35        0 8,20 * * *    bash /opt/.../deploy/job_alert.sh
#36        */30 * * * *    /opt/.../deploy/wrappers/job_pm_report.sh
```

---

## 6. 缺口与风险

### 6.1 环境变量依赖风险 (高)

**问题**: 所有 103 条实际 cron 依赖 `BASH_ENV=/opt/investment/MarketGraph/deploy/marketgraph_cron_loader.sh` (crontab 第 1 行)。

**影响**: tradingagent 独立 crontab 后不再共享此 loader。36 个任务需要各自通过 `env_loader.sh` 或 wrapper 内 `source` 自行加载环境变量。**缺失任何 PATH/PYTHONPATH/API_KEY 都会导致静默失败**。

**缓解**:
- 每个 wrapper 脚本统一 `source /opt/investment/tradingagent/shared/env_loader.sh`
- 迁移前在 staging 环境逐条 dry-run 验证
- 对比 `marketgraph_cron_loader.sh` 和 tradingagent `env_loader.sh` 差异

### 6.2 4 个直接 Python 调用需 wrapper 化 (中)

任务 #18, #21, #32, #33 当前直接调用 Python 脚本。迁移需要创建 wrapper 脚本并确保:
- Python 路径正确 (`/opt/marketgraph/venv/bin/python3` → 是否需要独立 virtualenv?)
- 日志路径从 MarketGraphRuntime 改为 tradingagentRuntime
- 与 MarketGraph 工具目录的代码引用关系 (gate_review.py 内可能 import MarketGraph 模块)

### 6.3 job_daily_brief.sh 双 cron 拆分风险 (中)

同一脚本 `job_daily_brief.sh` 被两个 cron (32 16 和 0 22) 调用。脚本内可能通过时间判断分派逻辑。拆分后需确认:
- 脚本是否内部分辨午/晚?
- 拆分后两个独立 wrapper 是否能保留原有分支逻辑?
- 是否需要单独参数传递 (`--mode midday` / `--mode evening`)?

### 6.4 PM 任务 30min 集群并发风险 (低)

6 个 PM/Crypto 任务在 `*/30` 整点同时触发, 瞬时 I/O 和 CPU 峰值。建议迁移时:
- 分散秒级偏移 (sleep 1-5s per task)
- 或改为 `0,30 / 3,33 / 6,36 / ...` 错开分布
- 封装为统一的 PM orchestrator 内部调度

### 6.5 weight_adjuster.py 库存遗漏 (低)

`crontab_remote.txt` 第 124 行的 `weight_adjuster.py --backtest-ok` 未出现在 `cron_inventory.csv` 的 101 条清单中。可能是迁移准备期间新增的条目。

**建议**: 确认归属 → MarketGraph (策略权重调整), 补充到更新后的 inventory。

### 6.6 gate_review.py 共用脚本 + 双 cron (中)

`gate_review.py` 被两条 cron 调用 (0 2 和 0 14), 仅通过时间分辨夜/日模式。日志分流用了不同文件 (gate_review.log vs gate_review_intraday.log), 但脚本本体相同。

迁移后需要两个独立 wrapper 或参数化调用 (`--mode night` vs `--mode intraday`)。

### 6.7 迁移顺序依赖 (高)

tradingagent 的 36 个任务依赖 SharedSignals 的数据采集 (Tushare, RSS, Crypto, PM) 和 MarketGraph 的 regime/event_impact。迁移时必须确保:
1. SharedSignals 的数据采集 cron 先于 tradingagent cron 运行
2. MarketGraph 的 regime 刷新 (job_regime.sh, 15 9) 先于 tradingagent 的盘前信号 (job_premarket_signals, 25 9)
3. 迁移窗口选在周末, 确保无盘中中断

### 6.8 回滚风险 (中)

旧 MarketGraph crontab 被精简后, 如果 tradingagent 独立 crontab 出现问题:
- 已删除的 36 条旧 cron 需要恢复
- 或临时回退到 MarketGraph crontab 的旧条目

**建议**: 迁移前备份完整 crontab (`crontab -l > crontab_backup_$(date +%Y%m%d).txt`), 保留旧条目注释而非直接删除, 先用 `#` 注释禁用观察一周。

---

## 汇总

| 维度 | 结论 |
|------|------|
| 36 任务覆盖 | **100%** — 全部映射到实际 crontab, 无缺口 |
| Cron 表达式一致性 | **100%** — 所有计划 cron 与实际 cron 完全匹配 |
| 未覆盖旧 cron | **66 条** — 25 MarketGraph + 40 SharedSignals + 1 未入清单 |
| 频率异常 | **1 个中等关注**: PM/Crypto */30 集群 6 任务同秒触发 |
| 需新建 wrapper | **4 个任务** (gate_review×2, self_heal 夜, daily_brief 晨) |
| 需拆分脚本 | **1 个任务** (job_daily_brief 午/晚) |
| 库存遗漏 | **1 条** (weight_adjuster.py) |
| 迁移风险等级 | **中高** — 环境变量、依赖顺序、回滚均需预案 |

**建议下一步**: 先创建 4 个缺失 wrapper 脚本, 补齐 env_loader.sh, 在 staging 环境 dry-run 验证, 然后选周末窗口执行迁移。
