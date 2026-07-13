# TradingAgent 数据与事实契约

> 本文是 SharedSignals/MarketGraph 输入、双市场资本、执行、样本、标签、KPI 与成熟度字段的 canonical contract。架构见 [architecture.md](architecture.md)，当前状态见 [STATUS.md](../STATUS.md)。

## 通用安全与 lineage

当前 capital-growth 记录至少明确：

```json
{
  "capital_authority_id": "ashare-capital-v1",
  "authority_generation": 1,
  "execution_lineage_id": "immutable-lineage-id",
  "point_in_time_as_of": "2026-07-13T09:35:00+08:00",
  "capital_layer": "simulated",
  "account_type": "simulated",
  "real_trading_enabled": false
}
```

- A股与 CNFutures 分别使用 `ashare-capital-v1` 和 `cn-futures-capital-v1`，不能交换 authority 或 lineage。
- source snapshot、receipt、local trade/position 和事件 payload 使用 SHA-256 指纹；时间必须带时区。
- 任一 real/live/direct execution、真实账户或实盘签名标记递归 fail closed，不能改写成 simulated 后继续。
- historical/legacy facts 只读保存；没有当前 authority + generation + lineage 的记录不进入当前 KPI 或成熟度。

## 上游输入

### SharedSignals

`SHAREDSIGNALS_API_URL` 是生产基础数据入口，由 `shared.data.reader.TradingagentDataReader` 消费。TradingAgent 不导入 SharedSignals 内部模块、不扫描兄弟仓目录、不打开其 SQLite，也不现场调用数据商。

输入包括 assets、日线/5 分钟行情、交易日历、合约元数据、基本面/因子/资金流/宏观/情绪、事件、行业 taxonomy/membership/snapshot 以及数据覆盖/新鲜度/source status。

行情是证据，不是交易信号。数据不可用、陈旧、缺来源或 PIT 不完整时，新增风险 fail closed；observation 仍保存并明确 data-quality/label eligibility。市场治理隔离：无关市场故障不能误停 A股或 CNFutures。

### MarketGraph

`MARKETGRAPH_API_URL` 只提供 regime、事件、行业/供应链传播等研究增强。它不提供账户、资本、订单或成交 authority。

paired 消融要求：

```json
{
  "base_snapshot_sha256": "64-hex",
  "marketgraph": {
    "enabled": false,
    "ablation_group": "mg_off",
    "applied_features": {},
    "overlay_status": "marketgraph_disabled"
  }
}
```

同一候选的 `mg_on` / `mg_off` 必须共享 base snapshot SHA、prediction time、基础数据质量、成本与标签口径；`mg_off.applied_features` 为空。

## 双 market capital contract

### 根与文件

| 市场 | 默认 root | event authority | latest projection |
|---|---|---|---|
| A股 | `shared/logs/capital/ashare/` | `ashare_sim_capital_events.jsonl` | `ashare_sim_capital_latest.json` |
| CNFutures | `shared/logs/capital/cn_futures/` | `cn_futures_sim_capital_events.jsonl` | `cn_futures_sim_capital_latest.json` |

环境覆盖分别为 `TRADINGAGENT_ASHARE_CAPITAL_ROOT` 与 `TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT`。不同市场 event files、locks、projections、roots 和 lineage 不得混用。latest 可从 append-only events 重建，不能覆盖 event authority。

### Policy

```json
{
  "market": "ashare",
  "capital_authority_id": "ashare-capital-v1",
  "authority_generation": 1,
  "initial_equity_cny": 50000,
  "single_name_max_pct": 0.15,
  "stock_gross_exposure_limit_pct": 0.90,
  "real_trading_enabled": false
}
```

CNFutures 使用相同初始权益和 generation，并以 `margin_utilization_limit_pct=0.50` 取代 A股单票/gross 字段。两个 policy 均为 `fresh_start_approved`，不接受 cross-market allocations 或 fixed protected cash。

### Snapshot/provider state

每市场 snapshot 至少包含：

- `source`、`authority_id`、`authority_generation`、`account_name`、`market`、`currency`；
- `initial_equity_cny`、`equity_cny`、`cash_balance_cny`、`positions_market_value_cny`、`margin_used_cny`；
- `frozen_order_cash_cny`、`frozen_order_margin_cny`；
- `reserved_cash_cny`、`reserved_exposure_cny`、`reserved_margin_cny`、`active_reservations_cny`；
- `available_to_reserve_cny`、`capital_utilization_rate`；
- `event_id`、`event_checksum`、`execution_lineage_id`、`reconciled`、`updated_at`；
- `positions_quantity_by_risk_unit`、`positions_cost_basis_cny_by_risk_unit`、`positions_entry_fee_cny_by_risk_unit`；
- `unreconciled_fill_commit_ids`、`real_trading_enabled=false`。

Provider 另包含 trade date/freshness、daily MTM/realized PnL、loss streak、high-water、drawdown limits 和本市场容量。A股提供 `single_name_cap_cny`/`stock_gross_exposure_limit_cny`；期货提供 `margin_utilization_limit_cny`/`available_margin`。总览只能并列，不能新增 combined equity/PnL/DD 字段。

### Opening/reconcile manifests

Fresh-start opening manifest 包含 market/authority/cutover decision、`mode=fresh_start`、50,000 CNY opening cash/equity、零继承持仓/预约/PnL、source SHA、execution lineage 和 `real=false`。初始化还必须验证真实 legacy freeze manifest；freeze 只证明旧源不可写，不导入旧数据。

MTM reconcile manifest 包含：

- actual cash、positions market value、unrealized PnL；
- per-risk-unit position margin、quantity/cost 的可验证来源；
- frozen cash/margin、exact active reservation manifest；
- `included_fill_commit_ids` 与 ledger 中未结 commit watermark 精确相等；
- authority/generation/execution lineage、PIT timestamp、source/SHA；
- expected ledger event ID/checksum。

任一金额、reservation、position、lineage、CAS 或 checksum 不一致都不得把账户标记 fresh/reconciled。

### Reservation

```json
{
  "market": "ashare",
  "reference_id": "stable-reference",
  "risk_unit_key": "600000.SH",
  "worst_case_cash_cny": 7500,
  "worst_case_exposure_cny": 7500,
  "worst_case_margin_cny": 0,
  "authority_id": "ashare-capital-v1",
  "authority_generation": 1,
  "execution_lineage_id": "...",
  "point_in_time_as_of": "...+08:00",
  "lineage_sha256": "64-hex"
}
```

相同 reference + 相同 payload 幂等；冲突 payload fail closed。A股 reservation 必须把该 symbol 的持仓市值、未决 reservation 和新订单合并校验 15% 及组合 90%；期货 reservation 使用 worst-case fee cash + margin，并另经止损预算门禁。

### Actual fill commits

共同字段：market、reference、risk unit、authority/generation/lineage、lineage SHA、order/idempotency/fill IDs、fill sequence、side/status/terminal、actual quantity/price、filled time、PIT、source/receipt/local-fact SHA、expected ledger event/checksum。

- A股买入 `fill_commit`：`actual_cash_debit_cny`、`actual_exposure_cny`、`actual_fee_cash_cny`，绑定 reservation ID/event/reference。
- 期货开仓 `fill_commit`：`actual_margin_cny`、`actual_fee_cash_cny`，绑定 reservation。
- A股卖出 `ashare_sell_commit`：actual closed quantity、gross proceeds、fee、net cash credit、gross realized PnL 和 local-position SHA。
- 期货平/减仓 `position_close_commit`：actual closed quantity、margin released、fee、gross realized PnL 和 local-position SHA。

partial 只结算 actual quantity；terminal 同一事件释放未使用 reservation。commit 成功或幂等成功前，fill 不得计入 execution-eligible 策略绩效。outbox pending 必须对新增风险保守可见。

## A股 capital plan

计划输出至少包括：

```json
{
  "deployed_utilization_rate": 0.3,
  "committed_utilization_rate": 0.3,
  "planned_stock_utilization_rate": 0.45,
  "dynamic_operating_cash_cny": 1000,
  "undeployed_capital_cny": 35000,
  "planned_undeployed_capital_cny": 27500,
  "undeployed_reasons": [],
  "position_capacity": 8,
  "remaining_position_slots": 5,
  "qualified_candidate_count": 12,
  "execution_eligible_candidate_count": 3,
  "automatic_promotion_enabled": false,
  "automatic_risk_expansion_enabled": false
}
```

`undeployed_reasons` 使用具体 code + amount + details，例如 dynamic operating cash、no execution-eligible candidate、single-name/gross limit、position capacity、insufficient lot/cash 或 safety blocker。不得用“样本不足”作为唯一未部署原因。

现金管理建议必须 `auto_order=false`、`status=suggestion_only`、`attribution_bucket=cash_management_yield`、`excluded_from_stock_alpha=true`。

## A股 prediction 与 exploration

每个风格 prediction 保存：

- `style_id`、`style_version`、lifecycle/hypothesis family；
- entry/exit thesis、holding horizon、direction；
- `raw_style_score`、`score_semantics=uncalibrated_heuristic`；
- `calibrated_probability=null`、`probability_model_state=not_calibrated`；
- `uncalibrated_return_prior` 与 model state；
- risk request、abstain/reject reason；
- capital authority scope、prediction snapshot/base snapshot SHA、MarketGraph ablation；
- forward-label request。

不得输出无校准证据的 `probability` 或把 prior 当作未来收益预测。

Exploration selection 保存：`exploration_policy_version`、top-K/pool、seed、selection method、`selection_probability`、`propensity`、chosen symbol/style/snapshot 和 not-selected reason。probability/propensity 必须相等且在 `(0,1]`。

订单归因保存 `sample_intent`、`primary_style`、`supporting_styles`、`style_scores`、`style_versions`、`decision_policy_version`、disagreement 和 exploration metadata。风格不拥有 capital account。

## A股本地执行事实

- 执行 authority：`shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/local_sim_trades.jsonl` 及同 root 的 manifest/outbox；旧 `shared/logs/local_sim/` 冻结且不参与当前统计。
- 签名回执：同一 fresh execution root 下的 `sim_execution_receipts.jsonl`；不得回退读取旧 `signals/` 或 `shared/logs/local_sim/` 回执作为当前 authority。
- 同日幂等键至少包含 market + account + trade date + symbol + side；多风格不得重复下单。
- execution-eligible fill 包含 actual positive price/quantity、timezone-aware timestamp、真实 5 分钟 source/timestamp/positive volume、candidate/execution source、100 股整手、T+1/session/limit/liquidity 与 capital commit identity。
- pending/unknown、请求值、弱价格证据或 commit pending 只能作为账户/chain-validation 事实，不能进入策略 PnL。

### A股 ExecutionRealityModel

当前版本是 `ashare-execution-reality-20260706-v1`，`effective_from=2026-07-06`。adapter、server-local 模拟撮合、共享 market-rules facade 和反事实成本必须读取同一模型，不得各自复制税费/涨跌幅/整手常量。模型依据沪深现行交易规则、中国结算交易过户费及现行印花税口径；规则变动必须发布新 model version，历史订单不得原地改口径。

- `price_limit_policy_version=ashare-price-limit-20260706-v1`：主板正常股及主板风险警示股涨跌幅均为 10%；科创板/创业板为 20%，北交所为 30%；上下限按 0.01 CNY tick 四舍五入。不得继续使用无板块语义的 `st=5%`。
- 买入为 100 股或整数倍；卖出不足 100 股的余额仅允许一次性卖出。`lot_rules.version=ashare-lot-rules-20260706-v1`。
- `session_policy_version=ashare-sessions-20260706-v1`：连续竞价只包括 `09:30–11:30` 与 `13:00–14:57`。`14:57–15:00` 是独立收盘集合竞价；`15:05–15:30` 是面向全部 A股的独立盘后固定价格交易，order type 为 `after_hours_fixed_price`，价格引用正式收盘价。当前同步模拟器没有集合竞价批量撮合或盘后固定价撮合，因此两者都必须返回显式 unsupported reason；不得延长普通连续竞价伪造成交。observation/counterfactual 仍继续记录。
- 连续竞价限价申报使用 `ashare-continuous-price-cage-20260706-v1`：买价上限为“基准价 102%”与“基准价 + 10 ticks”的较高者；卖价下限为“基准价 98%”与“基准价 - 10 ticks”的较低者。tick 为 0.01 CNY，执行样本保存可验证基准价来源。
- 撤单使用 `ashare-cancel-cas-20260706-v1`，保存 `state_version`、expected/observed state version 和 cancel outcome；成交/终态先到时不得回写成已撤。未来异步 broker 必须使用 append-only order events + startup reconcile，当前 sim-only 同步引擎不构成 broker-ready 证明。
- 卖方证券交易印花税为 5bps；交易过户费买卖双方各 0.1bps，分别保存 `stamp_duty` 与 `transfer_fee`，不得合并进 commission。当前保守佣金暂按 2.5bps、最低 5 CNY，`commission_schedule_status=provisional_pending_broker_contract`；它不是已核实的华创费率。只有实际合同/交割单核实后，才允许以 `broker_contract_verified`/`broker_statement_verified` 和独立 `commission_schedule_version` 覆盖。
- 成交费用记录至少包含 `commission`、`stamp_duty`、`transfer_fee`、总费用（模型为 `total`，本地成交/回执为 `total_fee`/`fee`）、`execution_reality_model_version`、`commission_schedule_status` 和 `commission_schedule_version`；实际成交绩效最终使用回执/交割事实，保守模型只用于模拟与反事实。

## SampleJournal

路径：`shared/review/ashare/sample_journal.jsonl`。每行 append-only、fingerprinted、拒绝 symlink/live markers；相同 identity + 相同 payload 幂等，冲突 payload fail closed。

A股 canonical intraday row 的 `bar_time` 是交易所本地时间、`collected_at` 是带时区的 provider receipt。写 prediction 前必须把无时区 `bar_time` 显式绑定 `Asia/Shanghai`；当上游没有更细的 `available_at/ingested_at` 时，只允许把已有 `collected_at` 同时作为保守 availability/ingestion receipt，不能生成更早时间。data-quality rejected prediction 永久保留为审计样本并在 KPI 显示排除数量，但不应永久污染同一 authority 后续有效样本的 scientific PIT denominator。

当前样本层：

| record/journal event | sample layer |
|---|---|
| prediction snapshot | `observation_counterfactual` |
| fill + exploration | `exploration_fill` |
| fill + exploitation | `exploitation_fill` |
| completed round trip | `completed_round_trip` |
| stop/exit | `exit_stop` |
| risk reject | `risk_reject` |
| weak/incomplete execution evidence | `chain_validation` |
| close-of-day authoritative MTM equity | `chain_validation` + `evidence_type=account_daily_mtm_equity` |
| label update | prediction 的 append-only label evidence |

5 分钟重复 cluster 的原始事件保留，但 KPI 权重只允许一个有效样本。只有当前 authority scope 进入 KPI；`excluded_legacy_event_count` 必须可见。

A股逐日正式回撤证据只在盘后固定价格交易结束后的 `15:31` 起写入。`ops` reconcile 以稳定日级 identity 向 SampleJournal 追加至多一条 `account_daily_mtm_equity`，保存账户权益、capital reconcile event、canonical snapshot SHA、PIT 时间链和当前 authority/lineage；更早的 opening/盘中 reconcile 只是资本 checkpoint，不得冒充收盘权益。仓库 cron 模板在 `15:32` 触发该 checkpoint，但模板未安装不等于运行证据存在。

独立样本单位不是 label cell。prediction 在写入时必须固定：

本组字段契约版本为 `sample_science_contract_version=ashare-sample-science-v1`。

- `decision_cluster_id`：同一 symbol、同一决策时点下的风格、MG on/off 与 horizons 共用一个 cluster；
- `primary_label_horizon` 与 `primary_horizon_policy_version=ashare-primary-horizon-v1`：结果发生前预先指定，当前 active style 使用 `1d`，防御/空仓 baseline 使用 `close`；
- `rank_score` + `score_semantics=uncalibrated_rank_score`：未校准时不得命名为 probability。

`sample_size_evidence` 同时输出：

- `ready_label_cell_count`：style×horizon 展示格数，仅诊断；
- `raw_N`：主 horizon ready 的预测行数；
- `unique_decision_cluster_count`：成熟度使用的独立 cluster 数；
- `independent_trading_day_count`；
- `N_eff`：按 cluster 去重并结合 propensity 权重计算的 Kish 有效样本量。

禁止使用 `ready_label_cell_count` 代替独立样本 N。

## Forward labels 与成本

规范 horizon：`m30, m60, close, 1d, 3d, 5d`；`next_day`/`next-day` 只映射到 `1d`。

每个 label 保存 target time、status、exit evidence、market/direction-adjusted gross return、cost model/version、fees/slippage 和 `net_return_after_costs`。状态至少区分 ready/labeled、pending-not-due、missing evidence、rejected data quality 与 rejected missing cost evidence。

- `as_of` 限制可见数据；日线不能伪造 m30/m60，晚到价格不能回填更早 horizon。
- 科学 PIT 证据必须同时保存并重新校验 `event_time <= available_at <= ingested_at <= retrieved_as_of <= prediction/label as_of`；source SHA 或任意 `as_of` 字段不能单独证明 PIT。缺这些时间戳不阻断 observation/label 写入，但该 cluster 不进入晋级证据。
- observation/counterfactual 使用版本化保守成本假设。
- actual round trip 使用真实 commission/stamp duty/transfer fee/slippage；缺 actual costs 不进入绩效或 promotion evidence。
- completed round trip 必须同时有有限数值 `gross_pnl_cny` 与 `net_pnl_cny`/`post_cost_pnl_cny`，不得把缺失值回落为 0 或静默用 gross-cost 推导。

## CNFutures session contract

每个有效会话记录至少包含：trade date/session、symbol/product、style/version、direction/side、raw score/prior semantics、regime/MG、holding horizon、PIT lineage、contract spec sources、size decision、counterfactual/execution class、hold/reject reason、label status 和 simulation-only flags。

不适配一手时 `quantity=0`、`counterfactual_only=true`。适配成交必须有 explicit `execution_eligible=true`、actual fill、capital commit identity 和 complete PIT lineage；正数量本身不能证明 execution-eligible。

## KPI 与成熟度 projections

`sample_kpi_latest.json` 按 style 和 sample intent 输出 counts、horizon statuses、completed round trips、win rate、average win/loss/PnL、expectancy、gross/cost/post-cost PnL、rejection reasons、missing evidence 与 scientific evidence。交易 PnL 序列的 `trade_pnl_sequence_max_drawdown_cny` 仅为辅助诊断；正式最大回撤来自 `account_drawdown_evidence` 的逐日 authoritative MTM equity 曲线。`shadow_capital_aggregated=false`。

`calibration_evidence` 必须从预先指定主 horizon 的独立 cluster 真实计算 Brier score、log loss、base rate、base-rate Brier、Brier skill 与 reliability bins/ECE。布尔字段或任一 chain row 不能自证 calibration。当前最低证据为 20 个独立 cluster、5 个独立交易日、正 Brier skill 且 ECE 不高于 0.15；不满足时 status 明确为 unavailable/insufficient，原始分数仍只称 `rank_score`。

benchmark 缺真实同期证据时，`benchmark_return/alpha/excess_return/beat_benchmark` 必须为 `null` 并带 `status=unavailable`；显式的真实 0 回报与 unavailable 是两种不同状态。

潜力股捕捉证据必须分开：`full_eligible_universe_recall`、`scanned_universe_recall` 与 `top_k_precision`。只有 full eligible universe 完整可证明时才允许声称全市场 recall；对被 `universe[:limit]` 截断的数据只能输出 `claim_scope=scanned_universe_only`，full-universe recall 为 null。

`market_maturity_latest.json` 至少包含：market/stage、authority scope、simulation day/count、checkpoint due、exploration eligibility、promotion evidence readiness、blockers/evidence summary、pilot bounds/status，以及：

```json
{
  "automatic_promotion_enabled": false,
  "automatic_risk_expansion_enabled": false,
  "live_transition_authorized": false,
  "real_trading_enabled": false
}
```

A股 stage 由交易日序号决定，第 5/10 日只标记 review due。期货 maturity 独立使用样本、品种/波动/会话、夜盘、换月、极端风险、费用后结果、回撤和稳定性，不使用 A股天数。

## 前端 contract

- `marketSummaries[]` 按市场保存 capital authority ID、generation、maturity 和市场自己的资本/PnL/return/DD；缺字段显示 null/unavailable。
- All Markets 不生成 combined monetary portfolio/performance；只可汇总非货币 counts/health。
- `portfolio.ashareAccount` 只显示 A股账户事实；CNFutures 使用自己的 market summary。不同市场的 capital、equity、PnL、return、drawdown、utilization 禁止聚合。
- 前端只读；不得创建/修改 signal、capital、sample、email、callback 或 execution state。

## 版本与变更

- schema、style、selection policy、decision policy、cost model、authority/generation 和 execution lineage 随记录保存；历史事件不原地补字段。
- 字段变更必须同步代码、测试、本契约和 [operations.md](operations.md)。
- 旧共享资本与旧演化入口没有兼容写路径；只读历史不得进入当前统计。
