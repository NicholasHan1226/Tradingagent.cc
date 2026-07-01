# 2026-07-01 运行时事件日志

本文记录 2026-07-01 当天发生的运行时事件、修复和事后复盘。这些是历史记录，用于理解某些运行时护栏的来源背景。当前权威规则见 [AGENTS.md](../AGENTS.md)。

## A股模拟执行事故链

### 事件1: 虚假成交确认（000002.SZ, 000006.SZ, 000007.SZ）

**时间线：**
- 2026-07-01 13:23 CST — 手动调度生成 20 个 A股模拟订单发送到 mini
- mini 因无法确认同花顺模拟盘模式，第一笔 000001.SZ 写回 failed
- 其余 19 个被并发迁移测试隔离到 mini 本地 `quarantine_migration_pending_20260701132459`
- 2026-07-01 13:39-15:29 CST — 修复 mini/Hermes 同花顺模式识别：优先用 AX 标签确认"模拟"，不再让 Vision 误判
- 事后复核发现 000002.SZ、000006.SZ 的早期"成交确认"实际是 false-positive：裁剪后的同花顺持仓表只显示旧持仓 600029
- 000007.SZ 属于点击提交但无严格确认的状态

**修复动作：**
- 000002/000006/000007 false-positive 记录已从 server signals/filled、positions、legacy receipt/ledger 中撤回
- 备份目录：`/opt/investment/agent_backups/ashare_sim_false_confirm_reconcile_20260701T152920`
- 000007.SZ halt 已归档到 mini `signals/halt_archive/executor_halt_20260701_160658_000007.SZ.json`

### 事件2: 过期 pending 清理

- 2026-07-01 15:35 CST — mini 上 16 条 13:32 批次 A股模拟 pending 已全部过期且未执行
- 在保留 executor_halt.json 前提下归档为 failed_final
- 同步失败回执到主服务器
- 备份目录：`~/.hermes/ashare-runtime/signals/backup_expired_pending_20260701_154334`

### 事件3: Stale pending cards 清理

- 2026-07-01 — 服务器端旧 A股模拟 pending cards（webhook bridge 之前）移至 `signals/expired`
- Cleanup manifest: `/opt/investment/agent_backups/ashare_sim_pending_cleanup_20260701113617.jsonl`
- 除非有意重放历史批次，不得移回 pending

## 修复记录

### Mini/Hermes 修复
- 16:03-16:07 CST — mini 只读模拟账户同步确认当前同花顺为"模拟练习"
- 持仓表只有 600029.SH 南方航空
- server positions 已按 mini read-only snapshot 对齐为 600029.SH 100 股
- mini health 当前 execution_status=ready

### 回执指纹闭环
- 23:04 CST — Mac mini `sim-signal-receiver.py` 写入 payload hash
- `sim-signal-executor.py` 写入 receipt hash
- 重启 `com.nicholashan.sim-signal-receiver` 与 `com.nicholashan.sim-signal-executor`
- 23:08 CST — 服务器端 push_remote_receipt 内联脚本：写入前验证 receipt_sha256/checksum，不匹配拒写

### 邮件与通知修复
- 邮件发送：Cloudflare Email Service REST endpoint
- 交易通道：`notice@tradingagent.cc -> tradingadviser@coze.email`
- 系统通道：`notice@tradingagent.cc -> soc@coze.email`
- 邮件模板渲染验证通过：trading_signal/daily_report/weekly_report/system_health

### 影子盘修复
- 影子信号不再写入可执行队列 signals/pending，统一写入 signals/shadow/pending
- A股影子账本拒绝非普通 A股代码（200xxx.SZ 等）
- 影子盘估值优先 SharedSignals 日线收盘价，缺失时回退最近影子成交价
- signals/shadow 具备完整状态目录：pending/claimed/running/filled/expired/cancelled/failed/partial

### PM/调度修复
- PM shadow scan 从每 5 分钟调整为每 10 分钟
- job_self_heal cron 日志判断只扫描最近 80 行
- job_pm_optimize 运行产物不再写入 Git 跟踪路径

## 事后规则（已纳入 AGENTS.md）

以下规则是从上述事件中提取的永久性护栏，已写入 AGENTS.md：

- mini health gate 检查 halted 状态
- A股交易时段保护（非交易时段 market_closed）
- 模拟执行订单同日幂等（market + account + trade_date + symbol + side）
- 截图确认只看裁剪后的持仓表区域
- 止盈止损不再以 "000007.SZ 点击未确认暂停" 状态消费队列
- 迁移测试和生产模拟交易任务隔离
