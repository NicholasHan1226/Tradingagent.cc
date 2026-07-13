# TradingAgent 当前状态

> 最后更新：2026-07-13 15:34 CST。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。

## 结论

- 本地重构已采用两个独立 fresh-start 50,000 CNY simulated authority；旧共享资金口径不再是当前设计。
- A股多风格 observation/exploration/exploitation、SampleJournal、forward labels、KPI、成熟度与原子资本 commit 已进入本地集成。
- CNFutures 独立资金、一手 affordability、会话样本、原子开/平仓资本 commit 和 durable outbox 已进入本地集成。
- 双市场 actual MTM reconcile writer、期货追加式 forward labels/maturity 与主页成熟度看板已完成集成；A股 `15:32 ops` 会在盘后固定价格交易结束后追加日级 MTM SampleJournal 证据。production managed cron 已通过项目 merge 工具完成 backup/dry-run/apply/readback，233 行 SHA 保持一致，coverage 56/56。
- A股执行现实统一为 `ashare-execution-reality-20260706-v1`：主板风险警示股 10%、独立收盘集合竞价/盘后固定价 session、价格笼子、100 股整手、卖方印花税 5bps、双向过户费各 0.1bps；华创佣金仍是待合同/交割单核实的保守临时口径。
- 样本科学口径已区分 label cells、raw N、unique decision clusters、独立交易日与 N_eff；PIT、校准、benchmark null、逐日 MTM 回撤和 universe recall 均不再由布尔字段或默认 0 自证。
- CNFutures 已加入 append-only order events、checksum projection 与 startup reconcile；目录漂移只 HALT 新增模拟风险，observation/counterfactual 继续。旧伪 Sharpe 已改为诊断比率，`sharpe=null` 且不得用于 DSR/晋级。
- `REAL_TRADING_ENABLED=false`；生产 systemd 已显式设置 `ASHARE_SIM_HERMES_ENABLED=0` 与 `ASHARE_SIM_WEBHOOK_ENABLED=0`，没有邮件、同花顺操作、broker 接入或真实交易。
- 2026-07-13 P0 修复已合入、push 并部署 production main `7db5a3c`：只读 capital 命令不再重写 latest；cron permission coverage 纳入两个 capital root、A股 execution root 与 CNFutures replay；新增 staging-only fixed-lineage/zero-import bootstrap CLI；SharedSignals gate 的 market 推断改为边界匹配，`opening_gate` red 现在会阻断 A股；A股 sample pipeline 将无时区 `bar_time` 绑定交易所时区并用真实 `collected_at` receipt 构建 PIT，present-but-invalid chronology fail closed。PIT/forward-label 相关 105 项通过；最终完整套件 1758 项通过、7 项时钟依赖失败与未改 `85fd3db` 基线的同一组失败一致。
- production 权限副作用已关闭：两个 capital latest 与 CNFutures replay latest/history 均为 `marketgraph:marketgraph 0600`；11:35 replay attempt=1 success。capital 只读命令不改 inode/mtime/SHA，cron coverage 为 56/56、runtime permission blocker=0。
- A股 production 已切换为固定 `ashare-sim-fresh-20260712-v1` 的独立 50,000 CNY authority 与 zero-import execution root；旧随机-lineage event root 整目录归档，旧 SHA 保持不变。隔离 reconcile 无 lineage mismatch，production 首次 `ops` reconcile 后为 2 events、fresh/reconciled=true、零持仓/成交/PnL、real=false；CNFutures 同时保持独立 50,000 CNY、fresh/reconciled=true。
- 当前仍不是“首日完整闭环完成”：A股 09:25/09:30 opening 已错过且不得补造，13:11 已实证按 red gate fail-closed；14:46 普通日内周期在 14:51:24 success，新增 2,000 条 prediction，其中 1,996 条 `eligible + PIT-complete`、4 条缺证据按设计 rejected，全部绑定当前 authority/lineage 且无 live marker。0 成交/0 回执并不影响 observation/risk-reject 样本成立；15:32 daily MTM 与 17:40 KPI/maturity 仍必须按实际时点验收。
- CNFutures 当日早盘因 release/source gate 未就绪而没有有效 `day_morning` 样本，禁止补造；`day_afternoon` session acceptance 已以 checksum verification 通过：495 条决策（494 hold、1 risk reject）、27 条 counterfactual-only、0 fill/real violation。风险拒绝样本包含完整 prediction、PIT、`cn-futures-capital-v1` generation 1 与 runtime lineage；一手 IF 保证金/损失预算超出 50,000 CNY authority 时保持 quantity=0。
- 15:32 A股正式日级 MTM 已由 managed cron 自主落地：SampleJournal 恰好一条 `account_daily_mtm_equity`，event time `2026-07-13T15:32:01+08:00`、equity 50,000 CNY，绑定当前 authority/generation/fixed lineage；capital chain 5 events valid，现金 50,000、持仓/预约/已实现与未实现 PnL 全为 0。17:40 sample ops 的 KPI/maturity 重建仍须按实际产物验收。

## 当前资本 authority

| 市场 | authority | generation | 初始权益 | 当前上限 |
|---|---|---:|---:|---|
| A股 | `ashare-capital-v1` | 1 | 50,000 CNY | 股票总敞口 45,000；单票 7,500；容量 8 |
| CNFutures | `cn-futures-capital-v1` | 1 | 50,000 CNY | 保证金使用 25,000 |

- 两市场各自持有现金、预约、持仓/保证金、MTM、PnL、回撤、checksum chain 与 execution lineage；总览不做货币聚合。
- 5% 回撤仅收紧到 0.75 倍风险预算；7% 回撤暂停。日亏与连续亏损也按市场独立触发。
- 旧资本事件、持仓和 PnL 已定义为只读冻结源；新 authority 不继承、不导入。production 两个默认 root 各 50,000 CNY、零持仓/预约/PnL、`real=false`；A股 active capital/execution lineage 已一致，旧随机 append-only root 只归档保留且禁止改写。

## A股本地证据

- 资金计划从 A股独立 policy 计算 90% gross、15% 单票、8 个仓位容量，并输出 deployed/committed/planned utilization、dynamic operating cash、undeployed capital/reasons。
- 四类正交风格共享候选、生成 counterfactual，并由单一组合账户消除同票重复成交。
- Exploration 使用 top-K epsilon/分层随机，记录 selection probability/propensity；每日最多一个、累计 7,500 CNY、日亏 225 CNY。
- 本地 fill/partial/sell 通过 durable outbox 与 capital ledger 原子提交，使用 actual quantity/price、commission/stamp duty/transfer fee、PIT lineage、receipt/local-trade fingerprints 和 ledger-head CAS；预约和成交账本读取同一 ExecutionRealityModel。
- A股 `SampleJournal` 保存 observation、exploration、exploitation、round trip、exit/stop、risk reject 与 chain validation；统一 sample ops 生成标签、KPI、manual-only evolution decision 和 maturity 投影。15:31 前的资本 checkpoint 不会冒充每日正式 MTM 回撤点。
- 第 5、10 个交易日只触发人工复核状态；自动晋级、自动扩风险和 live transition 均关闭。

尚缺的运行证据：下个有效交易日起连续每日 prediction、到期标签、探索/成熟成交、完整回合、资金利用率、未部署原因和 day-5/day-10 人工复核记录。

## CNFutures 本地证据

- 每个有效会话要求 prediction/candidate/hold/risk reject/simulated fill 之一，并区分方向预测与当前本金可执行性。
- 真实一手、保证金、费用、滑点、夜盘、换月和风险预算适配时才允许 execution-eligible simulated fill；否则保留 counterfactual。
- 开仓 `fill_commit` 与平仓 `position_close_commit` 使用 actual fill、margin、fee/PnL、PIT lineage、CAS 和 durable outbox；pending action 保守阻断新增风险并可重放。
- 期货 sample ops 会先追加 `m30/m60/close/1d/3d/5d` 标签，再按 exact authority/lineage、cluster 权重和 actual round-trip evidence 重建 maturity；无签名 summary、旧 authority 或重复 cluster 不计数。
- 期货成熟度已接入独立 runtime 投影和主页 read model，按品种、波动/会话、夜盘、换月、极端风险、费用后结果和稳定性展示，不读取 A股模拟天数。订单事件 projection 可从 append-only journal 重建；篡改或目录漂移会显式 HALT 新增风险。

尚缺的运行证据：不同有效会话和品种的连续记录、后续标签、真实规格模拟完整回合、夜盘/换月/极端场景覆盖与长期稳定性。

## 样本与演化 authority

- `shared/review/ashare/sample_journal.jsonl` 是 A股唯一演化事实源；KPI、decision 和 maturity 是可重建投影。
- paired MG 消融使用同一 immutable base snapshot；`mg_off` 不读取 MG 特征。
- heuristic score 与 uncalibrated prior 保持原名，不能包装为已校准概率或已验证预期收益。
- 自动 lifecycle/risk promotion 已关闭。证据 readiness 与 Nicholas 的人工授权是两个独立状态。

## 本地验证层级

- P0 固定聚焦套件：`719 passed`。
- 后端修复后全量：`1729 passed, 12 skipped`；skip 为既有条件性用例。
- fresh-lineage opening/ops 默认读取与机械格式化后的定向回归：`532 passed`。
- 前端：40 个 test files、`212 passed`；`npm run lint`、client build 与 API build 通过。
- 135 个变更 Python 文件的 compile/Ruff/format、shell syntax、`git diff --check`、21 个 Markdown 文件的本地链接、双 50k/sim-only/fresh-lineage authority 静态检查按固定矩阵通过。
- 测试 fixture、隔离临时账本和浏览器截图不是真实市场样本，也不证明策略正期望。

## 当前阻塞与下一门禁

1. A股普通日内首样本门禁已完成：14:46 周期新增 2,000 条 observation/prediction，1,996 条 eligible/PIT-complete；早先 17 条 risk reject 保留，0 fill。明确 `missed_opening=true`，不得补造 09:30 证据。
2. CNFutures `day_afternoon` 已通过真实有效会话验收，11:35 replay 有 636 windows/3 counterfactual rejects；`day_morning` 缺失与 opening 错过必须保留为缺口，不得补造。
3. A股 15:32 daily MTM、资金守恒与唯一日级 SampleJournal 已验证；17:40 sample ops 后验证 forward labels、KPI、maturity，未到时点只等待不伪造。
4. 只有上述运行证据、三仓文档、GitHub/production readback 与 rollback 证据齐全后才评估 worktree/本地分支清理；append-only ledger、样本、归档和运行证据不得删除。

## 环境层级

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | `main` HEAD `7db5a3c`；本轮 PIT/forward-label 105/105、gate 11/11、capital/bootstrap 176/176 通过；文档状态更新待本轮提交 |
| GitHub | `origin/main` 已回读为 `7db5a3c` |
| 生产文件/runtime | production HEAD `7db5a3c`；相关生产测试 105/105；服务/cron 用户为 `marketgraph`，sim-only env 明确；cron 每次加载当前 Python |
| 生产 cron | merge-tool backup/dry-run/apply/readback SHA 一致；56/56、missing/drift/env/root residual/permission blocker 均为 0 |
| 外部邮件/同花顺/broker | 未实现、未发送、未连接 |
| 真实市场样本 | 2026-07-13 opening 已错过且不补造；14:46 普通日内新增 2,000 predictions，1,996 eligible/PIT-complete、4 rejected、0 fill/live；15:32 唯一 MTM/50,000 CNY 守恒已验证；17:40 KPI/maturity 待实际产物 |
