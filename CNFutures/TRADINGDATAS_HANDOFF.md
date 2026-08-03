# CNFutures × TradingDatas 最小数据交接合同

> 状态：**未交接，不可运行真实模拟**。本文件是 CNFutures 的消费者验收规格；它不新增 TradingDatas 公共路由、采集器、定时任务或交易语义。

## 目的与范围

唯一可运行策略 `commodity_intraday_trend` 仅需要豆粕 `M` 的日盘 5 分钟研究数据。交接必须经 TradingDatas 的既有 `GET /v1/catalog` 与 `POST /v1/query` 完成；CNFutures 不直读 SQLite、不能调用上游，也不能依据 catalog 条目或 HTTP 200 认定可用。

`RB` 不是本次交接的执行范围。它将来只能另行以只读影子研究合同接入，且不得影响 M 的策略、资金、订单或成熟度。

## 最小数据集语义

TradingDatas owner 可选择符合其 provider-neutral registry 的最终 dataset ID；CNFutures 不固化或臆测 ID。交接中必须逐项给出最终 ID、schema major、查询 filter/sort、catalog version 和样例 receipt/lineage。

| 语义 | 必需字段或证据 | 说明 |
| --- | --- | --- |
| 可交易合约主数据与换月 cohort | contract symbol、product=`M`、exchange、list/delist 状态，以及消费者投影的连续 `effective_from`/`effective_until` tradeability 区间 | 只允许明确合约，不能使用抽象连续代码下单。consumer fixture 将 TD 的最终 list/delist 字段投影为 `tradeability={state:tradeable,trade_date,effective_from,effective_until}`；cohort 必须按有效时间连续、无重叠，并在决策时刻仅有一个 active contract。这不是假定 TD 原始字段名。 |
| 日盘 5 分钟 bars | contract symbol、bar end（含时区）、OHLC、volume、trade date | 每一查询行均须可追溯到 receipt/lineage；不消费未来可见 bar。 |
| 交易日历与产品会话 | trade date、calendar eligibility、session ID、`session_kind=day|night`、带时区且有序不重叠的 `session_windows`，以及 receipt-bound `authority={product:M,exchange:DCE,timezone:Asia/Shanghai,effective_windows}` | 午休、休市、收盘前 flatten 和换月均按此门禁；单一全天 start/end 不能替代交易段。authority effective windows 必须连续、无重叠，并在 decision time 恰有一个有效区间；night window 可从 trade date 前一自然日跨午夜至 trade date，但这只是调用方投影的 fixture 语义，不是静态交易所时段事实。 |
| 合约交易规格 | multiplier、tick、涨跌限制及适用日期 | 缺任一规格只能 hold/risk-reject，不生成 simulated fill。 |

保证金、手续费、生产端点、客户级限额与程序化报备不是 TradingDatas 的事实源；它们仍须分别经期货公司书面准入和未来 CTP/仿真测试确认，见 [REAL_BROKER_ADMISSION.md](REAL_BROKER_ADMISSION.md)。

## 消费前验收

每项验收都以同一时刻的 catalog 与 query envelope 为准：

1. catalog 中最终 dataset contract 可发现；这一步只确认合同，不确认数据可消费。
2. 对同一明确 M 合约读取两个相邻的、完整 5 分钟 bar；每根必须来自同一有效 trade date，且 bar end 连续、无重复、无未来可见数据。
3. 两次 query envelope 均满足 `ready`、`fresh`、`valid`、`degraded=false`，且有非空 receipt 与 lineage；任何一项失败则只保留 hold/observation。
4. 查询该会话对应的 calendar 和规格，并证明它们在策略 decision time 前可用；不能以静态 fixture、旧 bar 或消费者缓存替代。
5. 使用对方给出的精确 filter/sort/cursor 进行可终止分页 readback；consumer fixture 保留其 canonical `query_identity={filters,sort,cursor:null}`，最终 row identity、时间与 metadata 在幂等重读中一致。
6. 将上述 readback 投影为一次 CNFutures handoff fixture，并保存其 receipt/lineage。通过本消费者验收只代表 read-only parity 可复核，**不**启动 `delayed-paper`、runner、timer 或 simulated fill；它更不构成 SimNow、CTP、券商生产接入或实盘授权。

## 离线 profile fixture 验收

`CNFutures.tradingdatas_handoff_acceptance.evaluate_handoff_fixture` 是无网络、无数据库、无 runtime 副作用的 one-shot 消费者验收器。它只接受调用方从正式 `GET /v1/catalog` 与 `POST /v1/query` readback 提取的内存投影；不会配置 endpoint、token 或 dataset ID。

投影必须声明 profile `cn-futures-m-5min-handoff-v1`，并由 TradingDatas handoff 显式注入三个 role 的最终 dataset ID、schema major 与 `expected_contract_fingerprint`：`contract_master`、`bars_5min`、`calendar_session`。每个 fingerprint 都由共享 `shared.governance.evidence_readiness.dataset_contract_fingerprint` 对该 role 的 catalog row 重算并精确匹配；其公开材料固定为 `dataset_id`、`schema_major`、`default_fields`、`filter_operators`、`default_order`、`limits`、`identity_fields`，不由消费者复制 hash 算法。catalog 与每个 query 都须为 V1、`ready`、`fresh`、`valid`、`degraded=false`、完整 provider-neutral `metadata.lineage`、非空 `metadata.receipt_id`，并且 `next_cursor=null`。每个 query 还须附 consumer-preserved `query_identity={filters,sort,identity_fields,cursor:null}`：filter field/operator、sort 和 identity fields 都只能使用该 role catalog 合同已声明的字段；它绑定正式 readback 的筛选与排序，但不假定 TD 的原生 payload 字段名。`metadata.data_through` 与 `metadata.observed_at` 必须显式带时区，且满足 `data_through <= observed_at <= decision_time`；**唯一 availability source 是 `query_envelope.metadata.observed_at`**。验收器不接受 row 的 `available_at` 作为 provider-native knowledge-time：contract/calendar 的可用时点绑定各自 query 的 observed-at；bar 必须满足 `bar_time <= bars query data_through <= bars query observed_at <= decision_time`。输出保留 receipt watermark、query identity 与 canonical lineage digest，不要求或臆造 `lineage_ref`。随后才验证一个 receipt-bound M contract cohort：每个明确的 `M####.DCE` contract 由 TD list/delist 事实投影为包含 effective tradeability 区间的 `tradeability`。区间必须连续、无重叠，在 decision time 仅选择一个 active contract；该 active contract 的 `effective_from` 不得晚于同一 contract-master query 的 observed-at。随后才要求其非空有限的 multiplier/tick/price limit、同一上海 trade date 的有序 session windows，以及两根位于窗口内、整 5 分钟网格、相邻 completed、PIT 有效的日盘 OHLCV bar。

calendar row 还须注入 receipt-bound authority mapping：`product=M`、`exchange=DCE`、`timezone=Asia/Shanghai` 以及连续、无重叠的 `effective_windows=[{effective_from,effective_until}]`；其 active window 必须覆盖 decision time，且 `effective_from <= calendar query observed_at`。`session_kind=day` 的 window 必须完全处于同一上海 trade date；`session_kind=night` 可位于 trade date 前一自然日、trade date，或跨越这两个自然日，bar 的自然日也依此映射回同一 trade date。这个 mapping 是离线 fixture 的显式消费者合同，不是对 TradingDatas 原始字段、交易所夜盘安排或实时 calendar authority 的假设。

```bash
REAL_TRADING_ENABLED=false python3 -m pytest -q \
  tests/test_cn_futures_tradingdatas_handoff_acceptance.py
```

通过时仅产生 `observation`，并只在现有共享 `tradingagent.evidence_readiness.v1` 对应六项 envelope/contract/identity/receipt/lineage/quality 证明均成立时映射 `readiness.observation_ready=true`；它不意味着历史 PIT 或模拟执行可用。缺 `observed_at`/`data_through`/receipt/lineage、PIT 倒序、陈旧/降级/截断页、calendar/session 或 bar 证据时产生 `hold`；缺 multiplier、tick 或 price limit 时产生 `risk_reject`。三种结果均固定为 `historical_pit_ready=false`、`delayed_paper_ready=false`、`execution_eligible=false`、`learning_evidence_eligible=false`、无 durable capital/outbox。测试中的 `fixture.*` dataset ID 仅是 mock 标签，绝不是 TradingDatas authority 或未来 dataset ID。

## `fut_settle` 原始市场规则映射

`CNFutures.fut_settle_market_rules.load_fut_settle_raw_market_rules` 是独立、调用方触发的只读映射，不注册 runtime、timer、route 或持久化。调用方注入既有 `SharedSignalsV1Client`，并提供精确 `catalog_version`、`receipt_id` 与 lineage SHA-256。它只通过固定 `GET /v1/catalog` 和 `POST /v1/query` 读取 `cn.dataset.fut_settle`：catalog 必须为 schema major `2`、identity `[trade_date, ts_code]`，query 固定为单个 `trade_date`、`as_of=null`、`trade_date:asc,ts_code:asc`，并在受限分页内完成终页和同观察 replay。

映射仅保留 `M####.DCE` 行，且所有查询行的 `trade_date` 必须与请求完全一致。输出的 `settle`、交易费率/交易费、交割费和多空/套保保证金字段只是同一 receipt/lineage 绑定的原始事实；不推断 exchange rule、费率含义、保证金可用性或仓位。缺 identity、分区漂移、receipt/lineage/replay 漂移，或 metadata 不是 `ready/fresh/valid/non-degraded` 时 fail closed。该合同明确输出 `as_of=null`、`pit_authority=false`、`execution_eligible=false`，因此不构成稳定、PIT、shadow、模拟成交或真实交易授权。

## `fut_basic` 原始合约单位映射

`CNFutures.fut_basic_contract_units.load_fut_basic_raw_contract_units` 同样是 caller-invoked 的只读消费者。调用方注入既有 `SharedSignalsV1Client` 和精确 catalog version、receipt、lineage SHA-256；映射固定使用 `cn.dataset.fut_basic` schema major `1`、identity `[ts_code]`，并且只以 `fut_code=M` 过滤，绝不使用已知失败的 `exchange=DCE` 加 `fut_code=M` 复合筛选。查询固定选择 `ts_code`、`exchange`、`fut_code`、`multiplier`、`trade_unit`、`per_unit`、`quote_unit` 与 `quote_unit_desc`，以 `ts_code:asc` 在三页内读完 207 条，并要求同观察 replay 完全一致。

每行都必须是 `exchange=DCE`、`fut_code=M` 且 `ts_code` 唯一；缺 identity 或任一单位字段、receipt/lineage 漂移、非 DCE/M 行、非终页或 replay 漂移均 fail closed。它仅将 multiplier、trade-unit、per-unit 和 quote-unit 字段原样保留为 receipt-bound raw contract facts，不推导数字 tick、会话、PIT、换月或可执行规格。当前唯一接受的 metadata 状态是 `partial`/`degraded=true` 且唯一原因 `response_completeness_unverified`：输出强制 `coverage_complete=false`、`runtime_eligible=false`、`execution_eligible=false`、`trading_eligible=false` 和 `as_of=null`。因此它记录显式 coverage debt，不产生 stable、PIT、runtime、模拟成交或真实交易授权。

## M 合约 simulation-readiness 覆盖投影

`CNFutures.m_simulation_readiness.project_m_simulation_readiness` 是 caller-invoked、纯离线的每合约 coverage ledger。调用方只能注入现有 `fut_basic` raw-unit snapshot、`fut_settle` raw-rule snapshot、receipt/lineage 绑定的 `ft_limit` evidence，以及由 day/night handoff fixture 明确标记的 authority gaps；它不会创建 client、调用 API/provider、写入 runtime 或持久化。

投影按 `M####.DCE` identity 排序，逐项记录 raw-unit、raw-rule、price-limit raw fact 是否有对应 receipt-bound 行。它不解释任何 raw 值：`multiplier`、`quote_unit(_desc)`、fixture session window 与当前快照都不能生成 numeric tick、实时 receipt-bound session 或 PIT rollover authority。当前 ledger 固定保留 `fut_basic_coverage_incomplete`（其明细仍为 `response_completeness_unverified`）、`ft_limit_stale_or_degraded`、`numeric_tick_authority_missing`、`receipt_bound_live_session_authority_missing` 和 `pit_rollover_authority_missing`。只要存在任一缺口，projection 及每个 contract 的 `simulation_ready`、`runtime_eligible`、`execution_eligible` 与 `trading_eligible` 都为 `false`。

day/night 输入必须仍为 `fixture_only=true`；任何声称已具备 numeric tick、live session 或 PIT rollover receipt authority 的正向标记都会被拒绝，而不是在此投影中提升资格。因此该模块只形成 contract-ready 离线 coverage debt，不是 stable/PIT、simulation runtime、simulated fill、broker 或真实交易授权。

## 当前只读发现（2026-07-30）

已查到 registry 中 `cn.dataset.fut_basic` 可执行，候选分钟数据 `cn.dataset.ft_mins` 仍为 paused/blocked（缺基础 seed receipt），`cn.dataset.rt_fut_min` 仍为 paused/locked。它们不是本合同指定的最终 dataset ID，也不满足上述验收。因此当前结果为 **NO-GO：不启动 CNFutures runtime、模拟成交或 scheduler**。

## 交接后的顺序

1. TradingDatas owner 完成有界、只读 API readback，并提供最终合同与 receipt/lineage。
2. CNFutures 先执行上述离线 one-shot parity；这仍只是 fixture/mock-ready，不是 delayed-paper GO。
3. delayed-paper 必须另获 Nicholas 的运行授权，并在隔离 root 重新验证 observation、hold 与风险拒绝；不得由 fixture 测试自动触发。
4. 覆盖正常、缺数据、午休、收盘前、换月和波动状态后，才可考虑保存完整模拟回合与前向标签。
5. RB 影子研究、SimNow/CTP 测试和券商生产外接均为后续独立门禁，不能与此交接合并。
