# TradingAgent 当前状态

> 最后更新：2026-07-13 09:38 CST。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。

## 结论

- 本地重构已采用两个独立 fresh-start 50,000 CNY simulated authority；旧共享资金口径不再是当前设计。
- A股多风格 observation/exploration/exploitation、SampleJournal、forward labels、KPI、成熟度与原子资本 commit 已进入本地集成。
- CNFutures 独立资金、一手 affordability、会话样本、原子开/平仓资本 commit 和 durable outbox 已进入本地集成。
- 双市场 actual MTM reconcile writer、期货追加式 forward labels/maturity 与主页成熟度看板已完成本地集成；A股 `15:32 ops` 会在盘后固定价格交易结束后追加日级 MTM SampleJournal 证据。仓库 cron 模板已更新但未安装。
- A股执行现实统一为 `ashare-execution-reality-20260706-v1`：主板风险警示股 10%、独立收盘集合竞价/盘后固定价 session、价格笼子、100 股整手、卖方印花税 5bps、双向过户费各 0.1bps；华创佣金仍是待合同/交割单核实的保守临时口径。
- 样本科学口径已区分 label cells、raw N、unique decision clusters、独立交易日与 N_eff；PIT、校准、benchmark null、逐日 MTM 回撤和 universe recall 均不再由布尔字段或默认 0 自证。
- CNFutures 已加入 append-only order events、checksum projection 与 startup reconcile；目录漂移只 HALT 新增模拟风险，observation/counterfactual 继续。旧伪 Sharpe 已改为诊断比率，`sharpe=null` 且不得用于 DSR/晋级。
- `REAL_TRADING_ENABLED=false`；生产 systemd 已显式设置 `ASHARE_SIM_HERMES_ENABLED=0` 与 `ASHARE_SIM_WEBHOOK_ENABLED=0`，没有邮件、同花顺操作、broker 接入或真实交易。
- 2026-07-13 P0 本地修复已合入并 push GitHub main `773b0a5`：`status`、`dual-status`、`reconcile-dry-run` 与 `cutover-audit` 不再重写 capital latest 投影；cron permission coverage 已纳入两个 capital root、A股 fresh execution root 与 CNFutures replay。定向 143 项通过；全套 1751 项通过、3 项因测试把 10:00/10:05 行情放在本轮 09:07 当前时间之后而失败，无改动 `a558495` 基线精确复现同样 3 项失败。
- 生产曾同步到 `a558495` 并安装 sim-only service/managed cron，但新 P0 `773b0a5` 尚未生产同步。2026-07-12 20:53 fresh 证据确认两个 `*_latest.json` 已从误写的 `root:root 0600` 精确恢复为 `marketgraph:marketgraph 0600`，投影与 event SHA 均未变化；随后生产 SSH 在密钥交换阶段持续被远端关闭，CNFutures replay 权限、生产代码与后续门禁尚未处理。
- 当前不是“首日启用完成”：A股 production capital bootstrap 使用随机 `mcap-ashare-g1-...` lineage，但执行合同固定为 `ashare-sim-fresh-20260712-v1`，且默认 fresh execution root 不存在；09:25 硬切点已错过，不得声称捕捉 A股开盘，也不得补造期货 09:02/opening 证据。全部 P0 绿后只可从普通日内周期开始真实 PIT 模拟采样，并明确标记 missed opening。

## 当前资本 authority

| 市场 | authority | generation | 初始权益 | 当前上限 |
|---|---|---:|---:|---|
| A股 | `ashare-capital-v1` | 1 | 50,000 CNY | 股票总敞口 45,000；单票 7,500；容量 8 |
| CNFutures | `cn-futures-capital-v1` | 1 | 50,000 CNY | 保证金使用 25,000 |

- 两市场各自持有现金、预约、持仓/保证金、MTM、PnL、回撤、checksum chain 与 execution lineage；总览不做货币聚合。
- 5% 回撤仅收紧到 0.75 倍风险预算；7% 回撤暂停。日亏与连续亏损也按市场独立触发。
- 旧资本事件、持仓和 PnL 已定义为只读冻结源；新 authority 不继承、不导入。生产两个默认 root 已初始化为各 50,000 CNY、零持仓/预约/PnL、`real=false`，但 A股 active root 在首个有效样本前必须通过新隔离 root 重新建立与固定 execution lineage 一致的 authority；旧 append-only root 只归档保留，禁止改写。

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

1. 先恢复生产 SSH；当前连接在认证前的 `kex_exchange_identification` 阶段被远端关闭，不能用凭据、GitHub main 或外部 HTTP 替代生产写入/回读证据。
2. 同步 `773b0a5` 后，以 `marketgraph` 验证只读 capital 命令不再改写投影；精确恢复并验证 `shared/review/cn_futures/replay_{latest.json,history.jsonl}`，运行更新后的 cron permission coverage，禁止批量 chown。
3. 在没有首个有效样本前保留旧 A股 event root，使用项目 `market_capital_ops.py init` 在新隔离 root 建立固定 `ashare-sim-fresh-20260712-v1` lineage 的 50,000 CNY zero-position authority，再用唯一 zero-import execution bootstrap 建立同名执行 root；manifest/checksum/owner/mode/reconcile 全部通过后才可原子切换。不得改写旧 event 或手写 production ledger/lineage JSON。
4. SharedSignals DuckDB P0 已在其 main `2f2d881` 修复，但仍需生产部署并证明 16 表无失败/mismatch、58,698 条 event identity 完整、三行业空表 0 行合法且 source status 无 red。
5. 以上任一 P0 不绿继续 fail closed。恢复后只能验证普通日内 A股/期货真实 PIT 样本、15:32 closing MTM、KPI/maturity 与资金守恒；开盘证据已错过且不得补造。A股 Day 5/Day 10 仍只做人工复核，期货长期 sim-only。

## 环境层级

| 层级 | 当前事实 |
|---|---|
| 本地工作树 | `main` clean，HEAD `773b0a5`；P0 定向 143 项通过，完整套件 1751 passed + 3 个无改动基线同现的时钟依赖失败 |
| GitHub | `origin/main` 已回读为 `773b0a5` |
| 生产文件/runtime | 旧发布 `a558495` 曾运行；本轮 P0 尚未同步。服务此前为 `marketgraph` 且 sim-only env 明确，SSH 恢复后必须 fresh 回读 |
| 生产 cron | managed merge 已安装且旧覆盖报告 56/56，但该报告存在已修复的权限路径盲区；新 coverage 未生产运行，不能算绿 |
| 外部邮件/同花顺/broker | 未实现、未发送、未连接 |
| 真实市场样本 | 2026-07-13 opening 已错过且不补造；普通日内、closing MTM、KPI/maturity 尚待生产 P0 全绿后验证 |
