# TradingAgent / CNFutures

CNFutures 是国内期货的长期模拟研究与样本闭环。当前目标是持续保存方向判断、风险拒绝、反事实标签和真实规格模拟成交证据；它不承担 A 股资金加速，也没有实盘日期。

长期规则见 [AGENTS.md](AGENTS.md)。跨市场架构、字段、验收和运行方式分别见：

- [../docs/architecture.md](../docs/architecture.md)
- [../docs/data_contract.md](../docs/data_contract.md)
- [../docs/capital_growth_validation.md](../docs/capital_growth_validation.md)
- [../docs/operations.md](../docs/operations.md)

## 当前边界

- 唯一资本 authority：`cn-futures-capital-v1`，fresh-start simulated 50,000 CNY。generation 1 只是历史初始化基线；每轮必须读取、验证并传播 current snapshot 的正整数 generation，禁止写死。
- 最大保证金使用：25,000 CNY（50%）。保证金容量与止损损失预算分别校验。
- A 股与 CNFutures 的现金、保证金、PnL、回撤、execution lineage、样本和成熟度完全分离。
- `REAL_TRADING_ENABLED=false`。不得连接真实 broker、发送真实委托或自动切换 live。
- 5% 回撤只收紧风险预算，7% 回撤暂停；日亏和连续亏损门禁按期货账户独立触发。
- 静态合约规格只用于模拟 bootstrap，不能被描述为交易所级撮合、保证金或强平精度。

## 运行闭环

```text
TradingDatas catalog/query futures bars/spec evidence
  -> strategy prediction / hold / risk reject
  -> one-lot affordability and hard risk gates
  -> non-authority fixture simulated open/MTM/close math
  -> fixture-only sample/reconcile projection (no durable write)
```

每个有效会话至少保留 prediction、hold、risk reject、counterfactual 或 simulated fill 之一。样本不足不能阻断 observation；但数据、时段、最小一手、真实规格、费用、滑点、夜盘、换月、资金、回撤和 execution lineage 门禁不能为了采样而放宽。

方向预测与可执行性必须分开：不适配 50,000 CNY 账户的一手合约仍可形成 `counterfactual_only=true` 的方向样本和后续标签，但不能伪造成可执行成交。

## Fixture/mock 最小纵向切片

`CNFutures.fixture_closed_loop` 是当前唯一可直接运行的期货纵向切片。它只接收显式 `fixture_only=true` 的内存 mock，既不访问 TradingDatas、SQLite 或网络，也不声明或猜测真实 dataset ID。切片依次验证 fixture 数据证据、注入的 exchange trade-date/calendar eligibility、产品 session windows、换月保护、一手保证金/止损预算、多空开平、tick 对齐、费用、逐日 MTM、维持保证金强平风险、平仓和最终 reconcile，并输出独立的样本复盘记录与 lineage hash。

每个 bar、mark、close 必须各自提供显式带时区的 event `timestamp`、`available_at` 和 `decision_time`，允许且要求 `event_time <= available_at <= decision_time`（相等允许）；naive 时间一律 fail closed。整体要求 entry decision 严格早于 mark event，mark decision 不晚于 close event。这样允许正常发布延迟，但 pre-event、future-available、倒序或缺时刻均 fail closed；强平可按 mark 成交但仍需 close 时间证据。contract/calendar 的 `available_at` 也必须显式带时区且在 entry decision 前可用。entry、mark、close 都必须有各自的 `exchange_calendar`，其中 `trade_date`、`calendar_eligible`、`session`、`available_at` 必填；周日/休市、session mismatch 或跨 trade-date 的 follow-up evidence 不得形成成交或 reconcile。合约 symbol 规范化接受 `RB2610.SHF`，但必须与 product、最小 fixture product-exchange mapping 和月份 `01..12` 一致；真实完整规格仍等待 TradingDatas handoff。

fixture 数据证据必须精确声明 `GET /v1/catalog` 与 `POST /v1/query`，并同时保持 `ready`、`degraded=false`、`fresh`、`valid` 和非空 lineage；任何旧/provider route、degraded、stale 或 failed 状态均在候选和订单形成前 fail closed。canonical fixture 先生成稳定 `fixture_lineage_sha256`，再派生 `intent_id` 与不同的 open/close `order_id`，避免循环哈希；相同 fixture 重放的 ID 相同且不产生持久化或外部副作用。schema/dataset 仍等待 TradingDatas fresh manifest。

费用字段同时声明 `open_fee_type`/`close_fee_type`：`rate` 按成交名义金额计算，`fixed_per_lot` 按手数计算。两种费用以及静态/injected 规格都只是 simulation bootstrap，绝非真实交易所、期货公司或 TradingDatas authority。

资金 authority 每轮只读 `MarketPolicy.load("cn_futures")`：初始权益、保证金上限与 daily-loss budget 不在本模块复制常量。fixture `maximum_loss_cny` 只能收紧 canonical daily-loss budget；开仓前必须同时证明保证金、stop exposure、开/平预估费用和保留现金可支付。任一项不能满足时仅保留 counterfactual/hold，不能生成 execution-eligible order 或把负现金标记为 reconciled。

本切片永远不具有 execution authority：`candidate.execution_eligible=false`，只有通过数学和数据门禁时 `fixture_simulation_eligible=true`。订单仅为 `simulated_filled` payload，且显式 `execution_authority=false`、`durable=false`、`capital_commit_id=null`、`outbox_id=null`；对账仅为 `fixture_reconciled/non_authoritative`，不是 durable capital reconciliation。升级为 execution-eligible 前必须分别接入并验证 durable outbox、append-only capital ledger、原子 capital commit、execution lineage 及其 crash/reconcile 证据。

它的静态合约参数仅用于模拟 bootstrap，不能替代 TradingDatas 将来交接的可追溯合约规格。真实 handoff 到位前，任何非 fixture 输入都必须 fail closed；该切片不安装 cron、不连接 broker，也不写入 ledger/outbox 文件。

## 当前事实源

| 事实 | 路径 |
|---|---|
| 资本事件与 reconcile | `shared/logs/capital/cn_futures/` |
| 历史兼容持仓、回执与 durable outbox 投影 | `signals/`；time-boxed compatibility，不是跨市场或未来 live authority |
| 历史兼容 append-only 订单事件 | `signals/order_events/cn_futures_order_events.jsonl`；仅在 cutover 前按清单审计 |
| 历史兼容订单事件投影 | `signals/order_events/cn_futures_order_projection.json`；可重建、不可反向成为资本事实 |
| append-only 会话/样本 journal | `shared/review/data/cn_futures_sim_reviews.jsonl` |
| 当前成熟度/KPI 投影 | `shared/review/cn_futures/market_maturity_latest.json` |
| 只读观察报告 | `shared/review/cn_futures/observation_report.json` |

`market_maturity_latest.json` 只是可重建投影。只有 canonical `projection_sha256`、report type、evidence source、`cn-futures-capital-v1`、与 current snapshot 一致的正整数 generation、非空 execution lineage、50,000/25,000 资金口径、来源 SHA、sim-only 标记和 manual-review-only 策略全部一致时，才可用于当前成熟度展示；任一字段被改写后，观察报告和健康检查都会 fail closed。

## 演化规则

append-only review journal、前向标签、actual-cost execution evidence、Sample KPI 和成熟度是当前唯一演化证据。旧自动调权、自动生成变体、自动晋级和自动风险扩张均已退役。

订单状态目录只是兼容投影：启动必须用 checksum-chain order journal 重建并核对。当前本地 IOC 模拟允许 `partial` 作为明确终态；schema 同时表达未来异步订单的非终态 partial/`REDUCING`，但 broker、邮件和同花顺流程仍是 design-only，未实现。CN review 没有同频净收益序列时不计算 Sharpe/DSR；`net_pnl_to_drawdown_plus_fee_ratio` 只作诊断，永不作为晋级证据。

- runtime 不读取旧自动覆盖结果；
- 当前成熟度只做 assessment，不写策略、不调仓、不生成订单；
- `promotion_evidence_ready` 只表示证据检查结果，不构成任何实盘或扩风险授权；
- CNFutures 长期保持 `manual_review_only_no_futures_live_date`；
- 旧自动演化、旧 SharedSignals 专用检查及其 wrapper/schedule 已退出当前链；部分源码名或入口仍作为 fail-closed tombstone、fixture regression 或安装态法证线索保留。它们不得调度、联网或恢复成 fallback，只有 `legacy_inventory.yaml` 的消费者、安装态、parity 和回滚门全部通过后才物理删除。

## 本地只读/模拟检查

以下聚焦测试只使用本地测试输入，不安装 cron、不访问 TradingDatas 或连接真实交易：

```bash
REAL_TRADING_ENABLED=false python -m pytest -q \
  tests/test_cn_futures_execution_evidence.py \
  tests/test_cn_futures_sim.py \
  tests/test_market_lane_governance.py
```

`shared.runtime_test.cn_futures_live_check` 是旧 SharedSignals 路由的退役/法证入口，不是当前验收器；fresh TradingDatas handoff 前不得运行它，也不得把 `127.0.0.1:8082`、`/realtime_5min` 或其它 provider 专用路由恢复为默认值。

以下文件名只用于识别历史服务器安装态与退役依赖，不是当前推荐运行入口：

- `shared/wrappers/job_cn_futures_sim.sh`
- `shared/wrappers/job_cn_futures_sample_ops.sh`
- `shared/wrappers/job_cn_futures_observation_report.sh`
- `shared/wrappers/job_market_capital_reconcile.sh cn_futures`

仓库中的 wrapper 与 crontab 只是 tombstone/模板/法证线索；文件存在不等于可运行，更不等于生产 cron 已安装或 runtime 已切换。生产初始化、reconcile、cron apply、部署和发布均需单独 preflight 与 Nicholas 授权。

## 验收重点

- 当前交易会话有 prediction/hold/reject/fill 事实，不能静默零样本。
- 反事实与可执行成交分层，费用/滑点后的结果不与方向样本混算。
- execution-eligible fill 有实际 fill、手续费、滑点、合约规格、资本 commit 和 PIT/source SHA。
- partial fill、平仓、夜盘、换月、极端风险和 crash replay 可复盘。
- 成熟度按品种、波动、会话、夜盘、换月、极端风险、费用后结果、回撤和稳定性独立展示。
- 观察报告与健康检查忽略任何旧自动演化覆盖文件，且始终显示自动晋级/风险扩张/live transition 为关闭。

当前本地与生产状态只看 [../STATUS.md](../STATUS.md)，不能把本地测试通过表述为已部署、已安装 cron、已积累真实市场样本或已验证盈利。
