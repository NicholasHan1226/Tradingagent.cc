# TradingAgent Cron Migration

> **⚠️ 本文件是迁移记录。** 状态（"未安装 crontab, 未停远端旧任务"）可能已过时。当前权威状态以 [../STATUS.md](../STATUS.md) 和服务器 crontab 为准。

更新时间: 2026-06-30  
适用目录: `TradingAgent/shared/`  
状态: 本次仅生成迁移文件, 未安装 crontab, 未停远端旧任务

## 结论

- TradingAgent 归属: `36` 条。
- MarketGraph 保留: `25` 条。
- SharedSignals 归属: `40` 条。
- 遗漏待补: `1` 条 `weight_adjuster.py`。
- 迁移顺序: `SharedSignals -> MarketGraph -> TradingAgent`。

说明:
- 本地 `shared/cron_inventory.csv` 与 `crontab_remote.txt` 可验证到大批 active job。
- 上述 `36/25/40/1` 采用 `cron_gap.md` 指定归属口径。
- `BASH_ENV`、`SHELL` 属环境行, 不计入可执行 cron 数。

## TradingAgent 36 条清单

1. `job_trading_signals`
2. `job_premarket_signals`
3. `job_ashare_sim_exec`
4. `job_us_premarket`
5. `job_us_hourly`
6. `job_us_shadow_exec`
7. `job_us_postclose`
8. `job_crypto_shadow_exec`
9. `job_crypto_daily`
10. `job_crypto_weekly`
11. `job_pm_shadow`
12. `job_pm_forward`
13. `job_pm_optimize`
14. `job_pm_promote`
15. `job_auto_position`
16. `job_pm_risk`
17. `job_stress_test`
18. `job_gate_review_night`
19. `job_daily_brief_day`
20. `job_daily_brief_night`
21. `job_gate_review_day`
22. `job_weekly_review`
23. `job_us_weekly`
24. `job_us_signal_review`
25. `job_cross_market_review`
26. `job_strategy_attribution`
27. `job_factor_attribution`
28. `job_strategy_version`
29. `job_backtest_report`
30. `job_research_report`
31. `job_self_heal`
32. `job_self_heal_night`
33. `job_daily_brief_morning`
34. `job_email_notify`
35. `job_alert`
36. `job_pm_report`

## 依赖与迁移顺序

### 1. SharedSignals 先迁

原因:
- TradingAgent 36 条中多数依赖 `quotes`、`events`、`klines`、`benchmark`、`health`。
- 如果 SharedSignals 仍挂旧路径, TradingAgent 新 wrapper 会不断进入降级或 placeholder 输出。

先迁内容:
- 行情/事件采集。
- RSS/Tushare/marketdata_db。
- health monitor / maintenance / refresh。

### 2. MarketGraph 再迁

原因:
- TradingAgent 还依赖 `regime`、`event_impact`、`forward_calendar`。
- gate / review / attribution 的部分旧逻辑仍在 MarketGraph 工具侧。

先迁内容:
- `mg_agent/*`
- `job_regime.sh`
- `postclose_review.sh`
- `_dashboard.py`, `_api_server.py`, `_auto_tune.py`

### 3. TradingAgent 最后迁

原因:
- 只有 SharedSignals 和 MarketGraph 上游稳定后, TradingAgent 独立 crontab 才不会变成空跑或噪声报警。
- 本次生成的 `shared/crontab.txt`、`shared/wrappers/`、`shared/env_loader.sh` 即为最后一跳落地材料。

## 停删清单

迁移完成并完成 smoke 后, 远端旧 crontab 里以下旧任务应停用, 避免 TradingAgent / MarketGraph 双跑:

- `0 20 * * 0 /opt/investment/MarketGraph/deploy/job_weekly_review.sh`
- `0 22 * * 1-5 /opt/investment/MarketGraph/deploy/job_daily_brief.sh`
- `32 16 * * 1-5 /opt/investment/MarketGraph/deploy/job_daily_brief.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_pm_shadow.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_pm_forward.sh`
- `0 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_pm_optimize.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_pm_risk.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_pm_report.sh`
- `15 9 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_us_premarket.sh`
- `*/30 10-14,22-23,0-4 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_us_hourly.sh`
- `35 16 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_us_postclose.sh`
- `30 20 * * 0 /opt/investment/MarketGraph/deploy/wrappers/job_us_weekly.sh`
- `0 */4 * * * /opt/investment/MarketGraph/deploy/wrappers/job_crypto_daily.sh`
- `0 */12 * * * /opt/investment/MarketGraph/deploy/wrappers/job_crypto_weekly.sh`
- `30 * * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_us_signal_review.sh`
- `0 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_cross_market_review.sh`
- `30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_strategy_attribution.sh`
- `45 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_auto_position.sh`
- `30 8 * * * /opt/investment/MarketGraph/deploy/job_email_notify.sh`
- `*/30 * * * 1-5 /opt/investment/MarketGraph/deploy/job_trading_signals.sh`
- `0 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_strategy_version.sh`
- `0 2 * * * /opt/marketgraph/venv/bin/python3 /opt/investment/MarketGraph/tools/gate_review.py --apply --json`
- `*/30 10-14,22-23,0-4 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_us_shadow_exec.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_crypto_shadow_exec.sh`
- `25 9 * * 1-5 /opt/investment/MarketGraph/deploy/job_premarket_signals.sh`
- `*/15 * * * * /opt/investment/MarketGraph/deploy/job_self_heal.sh`
- `30 7 * * * /opt/marketgraph/venv/bin/python3 /opt/investment/MarketGraph/tools/daily_brief.py --apply --json`
- `30 * * * * /opt/investment/MarketGraph/deploy/wrappers/job_factor_attribution.sh`
- `0 17 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_backtest_report.sh`
- `0 3 * * 6 /opt/investment/MarketGraph/deploy/job_stress_test.sh`
- `0 16 * * 1-5 /opt/investment/MarketGraph/deploy/job_research_report.sh`
- `*/5 9-15 * * 1-5 /opt/investment/MarketGraph/deploy/wrappers/job_ashare_sim_exec.sh`
- `*/30 * * * * /opt/investment/MarketGraph/deploy/job_pm_promote.sh`
- `0 14 * * * /opt/marketgraph/venv/bin/python3 /opt/investment/MarketGraph/tools/gate_review.py --apply --json`
- `30 2 * * * /opt/marketgraph/venv/bin/python3 /opt/investment/MarketGraph/tools/self_heal.py --apply --json`
- `0 8,20 * * * bash /opt/investment/MarketGraph/deploy/job_alert.sh`

## 回滚方案

### 回滚触发条件

- 新 TradingAgent crontab 安装后 1 个交易日内出现持续空输出。
- wrapper 大面积进入 retry queue。
- 上游 SharedSignals / MarketGraph 尚未完成迁移, 导致 TradingAgent 大量误报警。

### 回滚步骤

1. 停用新的 TradingAgent crontab。
2. 恢复远端旧 MarketGraph crontab 中已停的 36 条 TradingAgent 任务。
3. 保留 `shared/env_loader.sh` 与 `shared/wrappers/` 文件, 仅回退调度切换。
4. 检查 `BASH_ENV` 是否仍指向旧 `marketgraph_cron_loader.sh`。
5. 记录是哪类依赖导致回滚:
   - SharedSignals 数据未就绪
   - MarketGraph 输出未就绪
   - TradingAgent 本地入口未实现

## 当前未验证项

- 远端 `marketgraph_cron_loader.sh` 未能读取。
  - 验证时间: 2026-06-30
  - 失败原因: 当前环境禁止 SSH 出站, `Operation not permitted`
- `shared/env_loader.sh` 已按“兼容上游 loader + 不写明文密钥 + 显式补齐关键变量”方式实现, 但仍属于待远端验证假设。
- 本地 `shared/` 代码并未覆盖全部 36 个真实业务入口。
  - 已在 `shared/wrappers/tradings_cron_entry.py` 中把缺口标为 `planned_only`。
  - 这些 wrapper 可作为迁移骨架, 不能等同于已完成业务实现。
