# TradingAgent Batch 开发计划 v1

> **⚠️ 本文件是 2026-06-30 的一次性开发计划草案。** 内容已过时，仅供历史参考。当前权威状态以 [../../AGENTS.md](../../AGENTS.md) 和 [../../STATUS.md](../../STATUS.md) 为准。

> 2026-06-30 | 基于 handoff + 用户 12 项答复 | 待用户确认后启动
> 执行原则:Claude 只做架构+审核;kimi 蜂群做研究/信息收集/中小任务;codex(5.4/5.5+推理强度)做重型实现/review/diff;同 wave 内多线程并行。

---

## 0. 架构取定(基于用户 12 项,请确认或纠正)

| # | 议题 | 取定 | 依据 |
|---|---|---|---|
| A | 数据入口 | TradingAgent 从 **SharedSignals** 单一入口读数据;MarketGraph 研究结论(regime/event_impact)经 SharedSignals 或文件读;六维打分去掉硬编码 3 仓库路径 | item 3/8 |
| B | MCP | **暂不做**,文件/SQLite 通信(已验证可用);MCP 列入未来 | item 3 管道通畅 |
| C | 执行桥 | TradingAgent(服务器)生成信号卡/任务 → mini 上**独立 cron 拉取执行**(模拟盘用 a_share_simulated_trade_executor,实盘只发邮件+mini 只读同步账户);**废弃 hermes_bridge SSH 直发** | note+item 11 |
| D | 多市场代码 | **vendor 实体代码进 TradingAgent 子目录**(不保留 symlink),兄弟仓库归档 | item 9/10 |
| E | Archive | 建 `/opt/investment/_archive/`,旧系统逐项验证后归档 | item 10 |
| F | cron | 103 条按三仓库归属拆分,TradingAgent 的迁入 `tradingagent/deploy/`,旧的停删 | item 7/8 |
| G | 多空 agent 化 | **是**:bull/bear 双 agent+独立记忆/日志+agents.md+固定 JSON+多轮(≥2)+规则护栏(六维均<0.4 时 belief 上限 0.4) | item 1 |
| H | 2层结构 | **影子层+模拟层**(+实盘层手动);多风格=影子层并行多策略独立 P&L 对比复盘 | item 4/11 |

---

## 1. Wave 结构总览

| Wave | 主题 | 并行任务数 | 依赖 | item 11 阶段 |
|---|---|---|---|---|
| W0 | 确定性 bug 修复 + 旧系统/cron/策略盘点 | 7(4 codex + 3 kimi 蜂群) | 无 | 前置 |
| W1 | 架构基线设计文档 | 4(3 codex-5.5 + 1 kimi) | W0 盘点 | 前置 |
| W2 | 资金层隔离 + 调度层 + 账本并发 + T+1 | 4 codex | W1 设计 | 影子盘前置 |
| W3 | 多空 agent 化 + 复盘闭环 + 自愈 + 风控修复 | 4 codex | W2 | 影子盘闭环 |
| W4 | 多市场影子盘闭环 + 日2次复盘 | 5 codex | W2 调度层 | P1 影子盘 |
| W5 | 模拟盘闭环(Crypto/US/PM + A股经 mini) | 2 codex | W1-C 执行桥 + W4 | P2 模拟盘 |
| W6 | A股实盘(邮件+mini 只读同步) | 1 codex + 审核 | W5 | P3 实盘 |

每个 Wave 结束 Claude 审核产出再进下一 Wave。

---

## 2. Wave 0 — 确定性 bug 修复 + 盘点(立即可启,全并行)

### 实现类(codex,改远端 /opt/investment/tradingagent/)

**W0-1 [codex-5.4 medium]** 修 condition_generator + 条件持久化骨架
- 修 `shared/screening/condition_generator.py:11` `from datetime import datetime` → `from datetime import datetime, timedelta`,line 253 `datetime.timedelta` → `timedelta`
- 给条件加持久化:condition_id(uuid)/status(pending/triggered/expired/cancelled)/valid_until/trigger_evidence/版本,写 SQLite `data/conditions.db`
- 交付:修复+条件表 schema+迁移说明

**W0-2 [codex-5.4 medium]** 修 hermes_bridge KeyError + 命令注入
- `shared/execution/hermes_bridge.py:155,179` `result[stderr]`/`result[stdout]` → `result["stderr"]`/`result["stdout"]`
- SSH cmd f-string 拼接 → `subprocess.run` 参数化 + ts_code/price/order_id 白名单 schema 校验
- 标注 hermes_bridge 为 deprecated(执行桥将按 W1-C 重设计),不删除
- 交付:修复+参数化+deprecation 注释

**W0-3 [codex-5.4 medium]** 修裸 import + smoke test
- `shared/risk/heal.py` `shared/risk/patrol.py` `shared/portfolio/constructor.py` 裸相对名导入 → 包内相对导入(`from .pre_trade_check import ...`)
- 加 `tests/test_import_smoke.py`:遍历所有 shared/ 模块 import 不报错
- 交付:修复+smoke test

**W0-4 [codex-5.5 high]** 统一 position schema
- 现状:position_ledger 输出 `avg_price/cost_basis`,risk/exit 读 `cost/entry_date/high_price/thesis` → 止损失效
- 定义统一 `Position` schema:`ts_code/quantity/sellable_quantity/avg_price/cost_basis/entry_date/high_price/thesis/capital_layer`
- 在 ledger→risk/exit 之间加 adapter(或让 ledger 直接输出完整 schema)
- position_ledger 加 `sellable_quantity`/`entry_date` 字段(支撑 T+1)
- 交付:统一 schema+adapter+字段扩展

### 研究类(kimi 蜂群,只读,不改代码)

**W0-5 [kimi 蜂群×5]** 旧系统盘点(每仓库一路)
- 分别扫 `/opt/investment/{Ashare,Crypto,US,PredictionMarkets,Portfolio}/`
- 每模块按"符合新架构可直迁/可利用优化后迁/无效删除"三类分类,给理由
- 交付:5 份盘点报告 → 汇总成 `docs/migration_inventory.md`

**W0-6 [kimi]** cron 103 条逐条映射
- 读 crontab + `tradingagent/shared/cron_inventory.csv`
- 每条 cron 标注:归属(tradingagent/MarketGraph/SharedSignals)+ 对应 TradingAgent 功能 + 频率是否需调整 + 迁移/停删建议
- 交付:`docs/cron_migration_map.md`

**W0-7 [kimi]** 各市场 strategies/ 现状盘点
- 查 TradingAgent 各市场 strategies/ 为何空;旧系统各市场策略定义在哪(`/opt/investment/{Crypto,US,PredictionMarkets}/` 内)
- 交付:`docs/strategies_inventory.md`

---

## 3. Wave 1 — 架构基线设计文档(依赖 W0 盘点)

**W1-1 [codex-5.5 high]** 数据流基线 + 数据合同文档
- 定义 SharedSignals→TradingAgent 数据出口 schema(regime/events/factors/moneyflow/bars/sentiment 的字段/新鲜度/覆盖率/异常回退)
- 六维打分改造方案:去硬编码路径,读 SharedSignals
- 交付:`docs/data_contract.md`

**W1-2 [codex-5.5 high]** 执行桥重设计文档(取定 C)
- 设计:TradingAgent 生成信号卡 → SharedSignals storage(或 SSH 推)→ mini cron 拉取 → a_share_simulated_trade_executor(模拟)/邮件+只读同步(实盘)
- mini 端需要的脚本/cron 定义;账户类型校验;T+1 在哪强制
- 废弃 hermes_bridge 的迁移路径
- 交付:`docs/execution_bridge_design.md`

**W1-3 [codex-5.5 high]** 统一调度接口 + 自动化任务清单文档(取定 F)
- orchestrator 设计:阶段(premarket/intraday/postclose/weekly)+频率+依赖+失败重试+写入端
- 自动化任务清单:每个 cron 目的/频率/输入/输出/写入端/失败处理/归属仓库
- 交付:`docs/orchestrator_design.md` + `docs/automation_tasks.md`

**W1-4 [kimi]** Archive 方案
- 归档清单(哪些旧仓库/模块)+ 归档时机(TradingAgent 对应功能验证后)+ `_archive/` 结构
- 交付:`docs/archive_plan.md`

---

## 4. Wave 2 — 资金层隔离 + 调度层 + 账本 + T+1(依赖 W1)

**W2-1 [codex-5.5 high heavy]** capital_layer 贯穿 + 三层账本隔离(取定 A/H)
- `capital_layer=sim|shadow|real`+`is_real_money`+`source_of_truth`+`can_affect_real_risk` 设为强制字段
- 贯穿 execution_router/audit_trail/capital_ledger/position_ledger/review
- 三层账本分文件/分表;shadow 记录不得返 `executed=True`
- `get_cash_position()` 默认只查 real
- 交付:字段贯穿+三层隔离+测试

**W2-2 [codex-5.4 medium]** 调度层 orchestrator 实现
- 按 W1-3 设计实现 `shared/orchestrator.py`:驱动现有纯函数库的阶段/频率/依赖/重试
- 接入 cron 入口脚本
- 交付:orchestrator + cron 入口

**W2-3 [codex-5.4 medium]** CSV→SQLite + 文件锁
- capital_ledger/position_ledger/shadow_broker 迁 SQLite(WAL)+单事务写入
- 解决并发 read-modify-write 覆盖
- 交付:SQLite 迁移+并发测试

**W2-4 [codex-5.4 medium]** T+1 强制(取定 C)
- position_ledger 存 entry_date/sellable_date;用交易日历(非自然日)
- 卖出路径(reduce/close)前强制 `can_sell` 校验
- 修 `can_sell` 用交易日历
- 交付:T+1 强制+交易日历接入

---

## 5. Wave 3 — 多空 agent 化 + 复盘闭环 + 自愈 + 风控(依赖 W2)

**W3-1 [codex-5.5 high]** 多空辩论 agent 化(取定 G)
- bull_agent/bear_agent 分离,各带记忆(`memory/adversarial/{bull,bear}/`)+日志
- 多轮反驳(≥2 轮)+ 可选裁判 agent
- 固定 JSON 输出格式:bull_case/bear_case/belief_score/key_risks/dissenting_points/evidence_refs
- 规则护栏:六维均<0.4 时 belief 上限 0.4;dry-run 标记不可信不静默
- `agents/adversarial/AGENTS.md` 角色边界
- 交付:agent 化实现+agents.md+记忆结构

**W3-2 [codex-5.4 medium]** 复盘→调权闭环(取定 H)
- 复盘输出候选补丁队列(`data/weight_patch_queue.json`):建议权重+OOS 验证+样本数+冻结期+回滚字段
- 不自动写回 weights.yaml;人工确认后 apply
- 加 OOS/训练-测试时间切分;冻结期检查
- 交付:补丁队列+OOS+冻结期

**W3-3 [codex-5.4 medium]** benchmark 接通 + 自愈修复
- benchmark.py 调 benchmark_tracker.get_benchmark_return(),去 placeholder
- 自愈 handler 拆 intent_sent/verified_effect/failed/escalated;未验证不得 healed=True
- 交付:benchmark 接通+自愈语义修复

**W3-4 [codex-5.4 medium]** 风控修复
- 黑天鹅:行情自动检测(大盘跌幅/涨跌停家数比/VIX 替代)生成 bool 标志
- 相关性降权改累积(非覆盖);加最低权重门槛(如<1% 拒单)
- 交付:黑天鹅自动检测+相关性累积+最低门槛

---

## 6. Wave 4 — 多市场影子盘闭环(item 11 P1,依赖 W2 调度层)

**W4-1 [codex-5.4]** A股影子盘闭环
- 集合竞价 opening/closing_auction STUB 实现(gap/surge/VWAP)
- 条件监控接 orchestrator(5min);shadow 通道接通
- 交付:A股影子盘可跑

**W4-2 [codex-5.4]** Crypto 影子盘闭环(vendor 工具进 tradingagent/Crypto/,接 orchestrator)
**W4-3 [codex-5.4]** US 影子盘闭环(同上)
**W4-4 [codex-5.4]** PM 影子盘闭环(同上)
**W4-5 [codex-5.5]** 日2次复盘闭环(lunch 11:35 + close 15:30,真数据驱动,3 对比+归因+写回补丁队列)

---

## 7. Wave 5 — 模拟盘闭环(item 11 P2,依赖 W1-2 执行桥 + W4)

**W5-1 [codex-5.5]** Crypto/US/PM 模拟盘闭环(simulator 接 orchestrator,各市场 sim_broker)
**W5-2 [codex-5.4]** A股模拟盘经 mini 执行桥(信号卡→mini cron→a_share_simulated_trade_executor,不碰账户信息,OCR 双重校验模拟盘)

---

## 8. Wave 6 — A股实盘(item 11 P3,依赖 W5)

**W6-1 [codex-5.5 + Claude 严格审核]** A股实盘
- 实盘信号经邮件发用户执行;mini Hermes 只读同步账户信息
- 实盘硬边界合同(不可绕过):capital_layer=real 强制人工确认、T+1、涨跌停、最大损失、撤单权限
- 端到端只读验收"无法自动下实盘单"
- 交付:实盘通道(只发信号+只读同步,不自动下单)

---

## 9. 执行与审核流程

1. 用户确认本计划(含第 0 节取定 A-H)
2. Claude 启动 Wave 0:并行 launch W0-1~W0-4(codex)+ W0-5~W0-7(kimi 蜂群)
3. Wave 0 完成后 Claude 审核代码修复+盘点报告,产出 Wave 1 精化输入
4. 逐 Wave 推进;每 Wave 结束审核 gate
5. 实盘 Wave 6 需用户最终授权
