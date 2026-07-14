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

每次真实 HTTP response 都在 cache 前保存独立 `sharedsignals_response_lineage`，至少含 `transport=http_response`、endpoint 与带时区 `received_at`。provider 自带 `evidence_envelope` 或其中任一 group 结构非法时，原非法值必须原样保留供 Evidence Gate 拒绝；transport lineage 只能作为本次网络响应审计，不能覆盖、修复或洗白 provider lineage。cache 命中必须返回同一审计事实且不能再次发起 HTTP。

### Sector flow confirmation（shadow-only）

个股 `/capital_flow` / `moneyflow:*` 行的 scope 是 individual stock，只能描述为“个股资金确认”。资产上的 sector/industry 标签不能把个股净流入提升为板块净流入。

`sector_flow_confirmation` v1 是独立影子特征。on 侧只接受：

```json
{
  "scope": "sector",
  "sector_id": "801780.SI",
  "sector_name": "银行",
  "taxonomy": "SW2021",
  "snapshot_id": "immutable-snapshot-id",
  "net_inflow_cny": 320000000,
  "rank": 2,
  "event_time": "2026-07-14T09:35:00+08:00",
  "available_at": "2026-07-14T09:35:30+08:00",
  "source_snapshot_sha256": "64-hex"
}
```

- off/on 使用同一 `base_snapshot_sha256`、规范化 `decision_as_of` 和 `pair_identity_sha256`，`pairing_version=sector-flow-confirmation-pair-v1`；off 不读取 sector snapshot。只有请求/快照 identity 全部合格时才生成 paired identity；否则 `pair_identity_valid=false`、`pair_identity_sha256=null`，off/on 回执绑定同一个空 identity，不能把非法输入包装成合法配对。
- source SHA 不是格式声明。实现固定按 `scope,sector_id,sector_name,taxonomy,snapshot_id,net_inflow_cny,rank,event_time,available_at` 的 canonical JSON 重算 SHA-256，并使用 constant-time compare 与声明值比较。任一 payload 字段变化而 SHA 未同步必须 degraded。
- `event_time <= available_at <= decision_as_of`，三者必须可解析且带时区。`scope`、请求/快照两侧 `sector_id`、`snapshot_id` 与 `taxonomy` 必须在任何 `strip` 或其它转换前先满足 Python `type(value) is str`，trim 后仍须非空；请求/快照 `sector_id` 再做精确比较，空/空不能视为匹配。bool、int、float、list、mapping、`None` 和空字符串均不得隐式转换为 identity。`net_inflow_cny` 必须是 JSON/Python 原生 number，Python 合同为 `type(value) in {int,float}`，明确拒绝 bool、numeric string 和其它隐式可转数值类型，之后再校验 finite。rank 必须是 JSON/Python 类型级原生 integer 且 `>=1`：Python 合同为 `type(rank) is int`，明确拒绝 bool、所有 float（包括数学上等于整数的 `2.0`）及所有 numeric string（包括 `"2"` / `"2.0"`），不得先 coercion 再验值。缺快照、错 scope/sector、非法或空 identity、未来 availability、无时区、坏或不匹配 SHA、非法资金类型、NaN/Infinity、非严格整数 rank 全部 `status=degraded`、`confirmation=null`、`applied=false`、`consumed=false`。
- 当前 consumer 固定为 `shadow_observation_only` 且 `consumed=false`。消费回执必须逐项记录 `changed_candidate_membership=false`、`changed_ranking=false`、`changed_playbook=false`、`changed_strategy=false`、`changed_execution_eligibility=false`、`execution_gate_bypassed=false`，并保存内容相同的 `before_identity` / `after_identity`（base snapshot、decision time、pair identity）。
- 该特征没有资本、风险或执行 authority。未来如需影响候选、排名或策略，必须另行修改 decision consumer、定义可归因回执并重新通过既有数据/风险/执行门禁；本合同不构成该授权。

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

### A股当前持仓 authority view

A股在 planning/risk/rebalance 前把 capital provider state 固化为唯一可重放 view。除上述字段外，以下字段全部必填且必须内部一致：

```json
{
  "trade_date": "20260714",
  "authority_id": "ashare-capital-v1",
  "authority_generation": 1,
  "execution_lineage_id": "immutable-lineage-id",
  "event_checksum": "64-hex",
  "checksum_status": "valid",
  "checksum_last": "same-64-hex",
  "checksum_event_count": 3,
  "positions_quantity_by_risk_unit": {},
  "position_count": 0,
  "positions_fingerprint": "sha256-of-canonical-normalized-positions"
}
```

- `checksum_event_count` 是非 bool 的正整数；`checksum_last` 必须等于 `event_checksum`。任一 checksum 字段缺失、非法或不一致均 fail closed。
- `positions_quantity_by_risk_unit` 必须显式为 mapping；缺失不能解释为空仓。股票代码规范化后必须是六位代码加 `.SH`/`.SZ`/`.BJ`；别名规范化后重复也非法。数量必须是有限、非负整数，零数量从 canonical positions 中排除，负数、bool 和小数均非法。
- `position_count` 必须等于 canonical positions 的键数；`positions_fingerprint` 必须等于 canonical JSON 的 SHA-256。不得信任来源自报 count/fingerprint。
- source 与 final capital state 在同一门禁中双读；trade date、完整 capital state SHA 或 authority view checksum 任一漂移均视为并发绑定失败。

每个 server-local、adapter、strategy 或 generic snapshot 必须携带完整 position-source envelope：

```json
{
  "source": "server_local",
  "position_source_status": "ready",
  "positions": [],
  "authority_id": "ashare-capital-v1",
  "authority_generation": 1,
  "execution_lineage_id": "immutable-lineage-id",
  "authority_checksum": "64-hex",
  "trade_date": "20260714",
  "position_count": 0,
  "positions_fingerprint": "sha256-of-canonical-normalized-positions"
}
```

字段缺失与非空不等同样阻断；只接受上述 canonical 键，不接受 `capital_authority_id`、`capital_authority_checksum` 等别名补齐，也不得在读取 snapshot 后从 current capital state 反向绑定 identity。所有 envelope 的 identity、canonical positions、count 和 fingerprint 必须与唯一 authority view 全等。失败结果固定为 `capital_position_source_mismatch`，审计至少保留 source name/status、source SHA-256、authority/state checksum、execution lineage、声明值、重算值与 mismatch fields。失败后不得进入普通 position-capacity risk reject 或动态 capital/rebalance 计算。

server-local lot snapshot 与 PnL projection 必须分别有显式 positions mapping，规范化 quantity view 全等后才可形成 server-local envelope。调用方必须把预先验证的 authority A context 作为读取参数交给 native producer；`local_sim_ledger` 从 append-only trade facts 重放并生成 source-owned identity/count/fingerprint，adapter 仅透传这份 live envelope。open lot 同时输出 `oldest_open_date` 与同值 canonical `entry_date`，供 T+1 风险检查直接消费，不得由 wrapper 猜测日期。`shared.accounting.position_ledger.get_positions` 返回的裸 `list` 不包含 source-owned identity，A股 current gate 不接受它，也不得在读取后补 authority 字段。磁盘 reporting snapshot、缺 context、缺 positions、非法 row 或 blocked status 均不得在读取后绑定为 ready；若 adapter 同时暴露 `strategy_positions`，该 strategy view 需要自己的完整 envelope，不能借用 adapter 主 positions 的 count/fingerprint。

position authority validity 与 new-risk eligibility 分开发布。authority/source 全部验证后，日亏、连亏或 7% 回撤令 view 保持 `status=verified`、原 positions/count/fingerprint 不变，并输出 `new_risk_allowed=false`、`new_risk_reason=<capital blocker>`、`risk_multiplier=0`。buy/open/add 不进入普通 risk、position capacity 或 replacement buy；sell/trim/exit 使用 verified position detail 继续执行 T+1、幂等、成交和 capital commit。authority 缺失/陈旧/校验失败或 source mismatch 仍输出 blocked + 空 positions，并阻断全部方向。门禁通过后 capital plan 的 `cash_source=market_capital_authority`，available cash 取 `cash_balance_cny` 与 `available_to_reserve_cny` 的保守较小值；普通 risk 的 current total exposure 取已验证 capital state 的 `positions_market_value_cny / 50000`，不能因 adapter/source 缺 weight 而默认为零。其它来源 cash/weight 字段仅保留诊断。任何 `filled` 或 `partial` 持仓变化后的 post-execution refresh 必须重新双读并使用 `cash_source=market_capital_authority_post_execution`；新 source envelope 未同步或不一致时 refresh 为 blocked，不得回退 adapter cash/positions。

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

路径：`shared/review/ashare/sample_journal.jsonl`。每行 append-only、fingerprinted、拒绝 symlink/live markers；相同 identity + 相同 payload 幂等，冲突 payload fail closed。Journal 与对应 lock 在持锁读写临界区都必须是 single-link regular file：`lstat(path)` 与打开 FD 的 device/inode 相等且 `st_nlink=1`，写前、读后/写后仍保持同一身份。hardlink、path replacement 或非 regular file 一律在修改历史前 fail closed；新建普通文件仍允许。

sample ops 每轮必须先通过 `SampleJournal.read_frozen(as_of=...)` 固定一个不可变输入视图。cutoff 使用 evidence availability/receipt 时间，而不是仅看 prediction/event time；顶层和 `point_in_time_lineage`（包括 `timestamps`）内所有契约 receipt/availability 字段都要校验并取最晚值，任一存在但非法或无时区即 fail closed。frozen head 至少固定并输出：

- `data_as_of`；
- `journal_head_event_count` 与 canonical `journal_head_sha256`；
- `max_evidence_available_at`；
- `excluded_after_as_of_count`；
- 用于并发前缀校验的 source inode、字节数与原始前缀 SHA-256。

同一轮 labels、KPI、decision 与 maturity 只能读取该 frozen view。label writer 自身追加的事件作为显式 task-owned delta 合并；frozen head 后出现的未知 append 必须阻断本轮批量写入，由下一轮以新 cutoff 重建，不能静默混入。最后一批 label 返回后，publisher 还必须对 physical Journal fresh head 做最终 CAS，并持有 Journal 共享锁直到 current pointer 原子替换结束，关闭“最终校验后、发布前”的竞态窗口。批量 label append 每批 100–250 条，只允许一次锁、一次前缀校验和一次 fsync；稳定 event ID、append-only 历史、幂等 crash replay 与冲突 payload fail-closed 规则不变。

A股 canonical intraday row 的 `bar_time`/`trade_time` 是交易所本地时间、`collected_at` 是带时区的 provider receipt。写 prediction 前只允许按这一显式字段契约把无偏移 `bar_time`/`trade_time` 绑定 `Asia/Shanghai`；`prediction_at`、`data_as_of`、receipt/availability/ingestion 与通用 `timestamp` 必须原生带时区，非法、无时区或语义冲突一律 fail closed。reference timestamp、prediction 与 data-as-of 比较时统一换算为 UTC instant，不比较字符串或墙钟字面值；reference 不得晚于 data-as-of 或 prediction。

prediction 必须同时保存 reference/decision timestamp lineage，至少包括 source field、原始值、标准化值、时区语义、normalization rule、valid/reason。缺整个 lineage、缺任一必需字段、`valid!=true`、raw/normalized instant 不一致或 normalized instant 与 `data_quality.price_timestamp`/prediction/data-as-of 不一致时均不得成为 `verified_reference_data`；可补齐的缺失保持 `pending_reference_evidence`/degraded，已进入 candidate/snapshot 的 present-but-conflicting 证据 fail closed 为 data-quality rejection。raw source 不得被标准化值覆盖。A股日线仅允许把带明确 `trade_date` 语义的日期标准化为当日 `15:00 Asia/Shanghai`。provider/bar/reference 在任何归一化前必须构造 EvidenceEnvelope，保留所有 present event aliases 与 receipt/availability aliases 的原始路径和值；不能先取首个非空值再复制成四钟 lineage。embedded `structure_errors` 是不可逆审计事实，重复或嵌套 canonicalization 必须确定性继承并去重，不能被 root convenience fields 洗成 valid。collector 必须给每个原始 row 传入真实 prediction/decision boundary，先过滤 invalid、naive、冲突、future receipt 或字段不完整的 row，再从有效 rows 按 canonical event instant 选择 reference；provider 返回顺序和无效 sibling 的价格都不得控制结果。被过滤 row 只能进入独立 `rejected_sibling_evidence` audit，不能成为 candidate price/PIT lineage；若没有有效 row，reference price 为 null、snapshot 为 retryable pending/degraded、`data_quality.qualified=false` 且 exploration 不可 selected。receipt 顺序按所有 present aliases 验证：`event <= min(all receipts)`、`max(availability) <= min(ingestion)`、`max(ingestion) <= min(retrieval)`；缺 stage 只能从真实 present receipt 作保守派生。单个晚值不能掩盖同组较早的跨 stage 反序，任务 `as_of`、wall clock 或更早别名都不得补造。

缺失 `reference_price` 不得伪造价格，也不得写成 terminal data-quality rejection；prediction 保持 `pending_reference_evidence`，到期 label 保持可重试 `missing_exit_evidence/missing_reference_price` 并在 sample-ops 输出 degraded/retryable。非法价格、未来 reference、时区冲突或不可靠的已存在证据仍为 `rejected_data_quality`。data-quality rejected prediction 永久保留为审计样本并在 KPI 显示排除数量，但不应永久污染同一 authority 后续有效样本的 scientific PIT denominator。

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

`completed_round_trip` 只有通过统一 strict evolution validator 才能进入 maturity 或作为 `actual_execution_costs_v1`。同一 validator 必须从同一个 frozen Journal view 解析唯一 prediction、entry fill 和全部 exit stop：prediction append 必须保存最小 canonical `source_snapshot_payload`，validator 从权威 prediction event 重算该 payload 的 source SHA 和 canonical event content SHA；从 fill/stop 的明确 `execution_receipt_payload` 与 `execution_local_trade_payload` 重算 receipt/local-trade SHA；再从这些内容绑定 fingerprint 重算 round-trip source/content SHA。所有 supplied SHA 均使用 constant-time 等值校验，多腿 exit receipt/local-trade SHA 数组还必须与 `exit_fill_identities` 完全等长、同序，并按元素 constant-time 对应。64-hex 形状本身不构成证据。entry/exit identity、round-trip 数值和成本还必须与关联的不可变 fill/stop 逐项一致，显式非空 EvidenceEnvelope 与 PIT 四钟均 valid/aware/ordered 且不晚于本轮 cost boundary。任一 payload、hash、关联事件、字段、时间或顺序缺失/非法/future/conflict 时保留事件审计，但 actual cost 使用量为 0，并继续使用版本化保守成本；历史 prediction 缺少 source payload 时同样保守回退，不得补造。显式空/非法 envelope 不能由顶层 convenience fields 洗白，也不能从 wrapper、任务时间、prediction time 或 `as_of` 补造 receipt/source。

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
- 科学 PIT 证据必须同时保存并重新校验 `event_time <= available_at <= ingested_at <= retrieved_as_of <= prediction/label as_of`；source SHA 或任意 `as_of` 字段不能单独证明 PIT。reference/entry 与每个 exit candidate 都必须在排序、选价和计算收益前通过同一个 Evidence Gate，且 validation 必须 `complete=true,status=valid`。EvidenceEnvelope 在 record root、PIT root、PIT `timestamps` 与 adapter 原始 envelope 收集所有 present event aliases（包括 `event_time/source_event_time/timestamp/observed_at/bar_time/trade_time/datetime`）；它们必须换算到同一 UTC instant，同义 `+08:00`/UTC 允许，任一非法、naive 或冲突 fail closed。receipt/availability aliases 至少覆盖 Journal 的 21 条 root/nested 路径，并额外覆盖 provider `published_at/retrieved_at/collected_at_dt`；每个 present 值必须带时区且可解析，最晚证据时刻不得晚于本轮边界，较早字段不能覆盖较晚字段。validated envelope 的 canonical 四钟必须与 nested lineage 一致；窗口资格、排序、`evidence_at` 与写出 lineage 只使用该 canonical instant。任一顺序冲突、future receipt 或 canonical instant 超窗的 point 不能影响候选排序，也绝不能生成 `ready/verified_exit_evidence`。
- 原始 reference/entry collector 在选择前排除 PIT 失败 row；如果没有任何合法 row，候选保持 retryable `pending_reference_evidence`/degraded，不携带无效价格或 PIT。若一个已选中/已持久化的 reference/entry 声称有价格但其 lineage present-invalid，则为 `rejected_data_quality`。exit PIT 失败在可能由后续合法行情恢复时保持 retryable `missing_exit_evidence`/degraded。缺或非法 PIT 不删除 observation，也不伪造 terminal price/label；只有后来到达且独立通过 Evidence Gate 的合法 point 才能恢复该 horizon。
- CNFutures prediction writer 必须把 SharedSignals 实际 HTTP response receipt 连同 source event aliases、原始 bar 和 nested PIT 持久化到 immutable source snapshot；session review 与 forward-label adapter 必须原样传递该 envelope。合法 receipt 参与 prediction/data-as-of 边界，reference 与 exit 都可 ready；missing/invalid/naive/future/conflicting receipt 一律 non-ready。HTTP receipt 是 transport 实际接收事实，不得由任务 `as_of`、prediction/bar time 或当前墙钟代填。历史缺 receipt 的记录保持 pending/degraded。
- observation/counterfactual 使用版本化保守成本假设。
- actual round trip 使用真实 commission/stamp duty/transfer fee/slippage；缺 actual costs 不进入绩效或 promotion evidence。
- completed round trip 必须同时有有限数值 `gross_pnl_cny` 与 `net_pnl_cny`/`post_cost_pnl_cny`，不得把缺失值回落为 0 或静默用 gross-cost 推导。

### Projection generation identity

canonical generation ID 由唯一跨语言算法计算：取 `projection_input_sha256` 与恰好三项、按 filename 排序的 canonical projection SHA-256 map，编码为 compact recursively-key-sorted UTF-8 JSON 并追加一个 LF，再计算 SHA-256，前缀为 `ashare-sample-projection-`。publisher、Python reader 与前端 reader 都必须重算该 ID，并要求 pointer ID、directory basename、manifest ID 与重算值全等；manifest/pointer 即使重新签名也不能授权复制到任意伪造 generation ID。

若 content-addressed generation 目录已存在，publisher 必须在写任何 compatibility mirror 或 current pointer 前，使用与 active reader 相同的完整 validator 校验 exact 四文件集合、regular/no-symlink/no-hardlink、manifest 原始 SHA、三投影原始 SHA/JSON、共同 input lineage 与所有 sim-only 安全字段；manifest-only、缺文件、extra file、symlink、hardlink、可写 generation 或 hash mismatch 都是 collision/corruption。完整同内容 generation 才允许幂等复用。一次 publication 必须在 review root 独占协作锁内完成；generation 在可见前封存为目录/文件只读，validator 以 single-link file descriptor 读取并检查 inode/size/mtime/ctime 在读取期间未变。最终 generation validation 必须从同一次 FD validation 返回目录及 manifest + 三投影的 path、device、inode、mode、nlink、size、mtime_ns、ctime_ns 与 raw-content SHA-256 身份；pointer replace callback 重新验证完整内容后还必须与这份 final identity 逐项相等，不能把 content hash 相同视为同一个对象。三份 compatibility mirror 和三份 append-only log 在本轮写完后也必须分别保存相同字段的身份快照。pointer 临时文件 fsync 与最终 `os.replace` 均在该锁内；pointer replace callback 必须重新以 FD 读取 generation 和全部六份 compatibility 文件，并与各自快照逐项相等后才可切换。final validation 后发生 mirror 或 log 的 rename replacement、symlink、hardlink、内容/metadata 漂移，或 generation in-place/rename/hardlink/同字节不同 inode 替换任一变化，都必须使 publisher 失败并保持旧 current bytes 逐字节不变。reader 后续 fail closed 不能替代该 publisher 保证。

`.projection_publish.lock` 与 Journal lock 只约束已登记、遵守协议的授权 writers。合同不宣称能消除最后一次验证返回到 kernel rename 之间由非协作同 UID 写入造成的所有用户态 TOCTOU；该剩余面属于 P1 OS 隔离。任何生产启用必须附 writer inventory 与实际 readback：每个可写进程/cron/service 的命令、UID/GID，相关目录与文件的 owner/mode/ACL，mount options、filesystem 类型及 rename/link 语义。缺少任一证据或存在绕过锁的 writer 时，canonical publication 不满足生产门禁，sample-ops cron 继续禁用。

## CNFutures session contract

每个有效会话记录至少包含：trade date/session、symbol/product、style/version、direction/side、raw score/prior semantics、regime/MG、holding horizon、PIT lineage、contract spec sources、size decision、counterfactual/execution class、hold/reject reason、label status 和 simulation-only flags。

不适配一手时 `quantity=0`、`counterfactual_only=true`。适配成交必须有 explicit `execution_eligible=true`、actual fill、capital commit identity 和 complete PIT lineage；正数量本身不能证明 execution-eligible。

## KPI 与成熟度 projections

同一轮三份投影必须共享相同 `projection_input_sha256`，并通过内容寻址 generation 发布。完整 generation 写入 `projection_generations/<generation_id>/` 后，最后只原子替换 `projection_current.json`；pointer 必须保存 `generation_manifest_sha256`。canonical reader 先按该 SHA 校验 manifest 原始内容，成功后才信任 manifest 中的 projection SHA、共同 input SHA、run metadata 和 sim-only 字段，再校验三个文件。任一步在 pointer swap 前失败时，reader 继续看到上一完整 generation。generation 体系已存在或配置要求 canonical 时，current 缺失/非法必须 fail closed；`*_latest.json`/log 仅保留为向后兼容镜像，不是事务提交点。明确的 pre-generation legacy 健康检查回退必须标记 `legacy_compatibility_degraded`，不能输出成熟度绿或可晋级；活跃前端 reader 不使用该回退。

所有 canonical projection 必须显式保存 `real_trading_enabled=false`、`live_execution_enabled=false`、`automatic_promotion_enabled=false` 和 `automatic_risk_expansion_enabled=false`；decision/maturity 还必须显式保存 `live_transition_authorized=false`。字段缺失与字段为 true 同样 fail closed。

KPI、decision、maturity 与 sample-ops report 向后兼容新增：`data_as_of`、真实 wall-clock `generated_at`、`journal_head_event_count`、`journal_head_sha256`、`max_evidence_available_at`、`excluded_after_as_of_count`、`projection_input_sha256`、`run_id`、`H0` 和 `H1`。`H0={event_count,sha256}` 表示本轮 frozen canonical head；`H1={event_count,sha256,task_owned_delta_event_count}` 表示显式 task-owned label delta 后的本轮投影视图；未知外部 append 不得进入 H1。

既有污染投影只能通过 `projection_generation_audit.jsonl` 追加 `invalid` 或 `superseded` 审计事件；不得删除或改写旧 generation、Journal 或 ledger。本机制只提供代码级审计入口，不授权在生产历史上执行修复。

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
