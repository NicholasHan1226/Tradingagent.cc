# TradingAgent 当前状态

> 最后更新：2026-07-12。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。

## 结论

- 本地重构已采用两个独立 fresh-start 50,000 CNY simulated authority；旧共享资金口径不再是当前设计。
- A股多风格 observation/exploration/exploitation、SampleJournal、forward labels、KPI、成熟度与原子资本 commit 已进入本地集成。
- CNFutures 独立资金、一手 affordability、会话样本、原子开/平仓资本 commit 和 durable outbox 已进入本地集成。
- 双市场 actual MTM reconcile writer、期货追加式 forward labels/maturity 与主页成熟度看板已完成本地集成；A股 `15:32 ops` 会在盘后固定价格交易结束后追加日级 MTM SampleJournal 证据。仓库 cron 模板已更新但未安装。
- A股执行现实统一为 `ashare-execution-reality-20260706-v1`：主板风险警示股 10%、独立收盘集合竞价/盘后固定价 session、价格笼子、100 股整手、卖方印花税 5bps、双向过户费各 0.1bps；华创佣金仍是待合同/交割单核实的保守临时口径。
- 样本科学口径已区分 label cells、raw N、unique decision clusters、独立交易日与 N_eff；PIT、校准、benchmark null、逐日 MTM 回撤和 universe recall 均不再由布尔字段或默认 0 自证。
- CNFutures 已加入 append-only order events、checksum projection 与 startup reconcile；目录漂移只 HALT 新增模拟风险，observation/counterfactual 继续。旧伪 Sharpe 已改为诊断比率，`sharpe=null` 且不得用于 DSR/晋级。
- `REAL_TRADING_ENABLED=false`；没有 push、deploy、cron apply、邮件、同花顺操作、broker 接入或真实交易。
- 当前是“本地实现与固定验收已完成，等待单独发布授权”，不是已发布，也没有连续真实交易日的模拟绩效证据，不能宣称正期望或稳定盈利。

## 当前资本 authority

| 市场 | authority | generation | 初始权益 | 当前上限 |
|---|---|---:|---:|---|
| A股 | `ashare-capital-v1` | 1 | 50,000 CNY | 股票总敞口 45,000；单票 7,500；容量 8 |
| CNFutures | `cn-futures-capital-v1` | 1 | 50,000 CNY | 保证金使用 25,000 |

- 两市场各自持有现金、预约、持仓/保证金、MTM、PnL、回撤、checksum chain 与 execution lineage；总览不做货币聚合。
- 5% 回撤仅收紧到 0.75 倍风险预算；7% 回撤暂停。日亏与连续亏损也按市场独立触发。
- 旧资本事件、持仓和 PnL 已定义为只读冻结源；新 authority 不继承、不导入。生产默认 root 尚未由本工作树初始化或切换。

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

1. 仓库模板尚未安装到服务器，两个生产 root 尚未初始化或切换，SharedSignals 真实 runtime mark/reconcile smoke 尚未执行；这些动作需要单独发布授权。
2. 下个有效交易日起验证仓库外真实运行证据：每日 A股 prediction/探索或具体硬拒绝/标签/回合/closing MTM，以及长期 CNFutures 多会话、多品种和极端场景覆盖。
3. A股 Day 5/Day 10 只做人工复核；没有 Nicholas 明确确认不得进入 20%–30% 人工试运行。期货继续长期 sim-only。
4. 已冻结的生产启用、进化终端、邮件/同花顺规格与长期统计工作见 [docs/BACKLOG.md](docs/BACKLOG.md)，不在本任务继续实现。

## 环境层级

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | `codex/capital-growth-engine`，固定验收通过；本任务按逻辑切片只做本地提交 |
| GitHub | 本任务不 push；本地提交不能推断为远端分支或主线已存在 |
| 生产文件/runtime | 未同步、未重启、未切换 authority |
| 生产 cron | 未 apply；仓库模板不代表用户 crontab 已生效 |
| 外部邮件/同花顺/broker | 未实现、未发送、未连接 |
| 真实市场样本 | 新架构尚待下个有效交易日连续验证 |
