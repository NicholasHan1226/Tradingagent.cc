# TradingAgent 数据与事实契约

> 本文是 TradingDatas/MarketGraph 输入、按市场与原生币种隔离的资本、执行、样本、标签、KPI 与成熟度字段的 canonical contract。架构见 [architecture.md](architecture.md)，当前状态见 [STATUS.md](../STATUS.md)。

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

### 分层 readiness 与 authority

机器合同 [`shared/governance/evidence_readiness.yaml`](../shared/governance/evidence_readiness.yaml)
把原先容易混用的单一 `ready/learning_eligible` 拆成四个独立角色：

- `observation_ready`：只允许审计、当前观察和零名义 shadow；
- `historical_pit_ready`：另需首次可见时间、revision/immutable vintage、as-of 与标签窗口证据，才允许离线学习；
- `delayed_paper_ready`：另需已完成市场事件、延迟策略、下一事件成交、资本 authority、幂等与对账，才允许模拟新风险；
- `execution_ready`：当前阶段固定关闭，即使调用方把所有 proof 都置为 true 也不能取得真实执行权限。

这四项不是一个递增的“总成熟度”。历史 PIT 与 delayed-paper 在
`observation_ready` 之上分别取证；全天/24 小时连续性属于运行成熟度，不反向授予
历史学习、模拟资金或真实执行 authority。A股 48/48 仍是全天 feature、全天 KPI
与运行成熟度门禁，但预注册 feature/label profile 所需的局部连续窗口可形成独立
历史学习候选，禁止跨 gap。Crypto 288 个连续 5 分钟槽仍是自动运行成熟度门禁，
不是每个 append-only 离线样本的全局前置；每个学习样本仍必须单独证明连续窗口和
完整标签，gap 必须切断 segment，Challenger 仍只建议且人工晋级。

分钟 freshness 也按用途分层：execution-equivalent 继续固定最多 30 秒；
delayed observation 最多允许一个 bar cadence 加 30 秒 jitter，且禁止同 bar
成交；historical PIT 不按当前墙钟判 stale，但必须由 immutable receipt、as-of、
首次可见与 revision/vintage 证据证明当时可知。放宽观察延迟绝不放宽真实执行。

A股 500 股完整 cohort 仍是 delayed-paper 新风险的硬门禁。`>=99%` 的预注册
cohort 只允许零名义 shadow，必须输出精确 missing identity set，禁止静默补票、
替换或生成模拟订单。routine query 使用 receipt-bound 单次终端遍历；仅 onboarding、
逐 dataset 合同漂移、事故恢复和每日 full scrub 执行完整 same-observation 双遍历。

合同漂移的目标绑定是 `api_major=v1 + per-dataset contract fingerprint`。全局
`catalog_version` 继续作为 evidence 保存，但只新增无关 dataset 时不应阻断已冻结
dataset；fingerprint 至少覆盖 dataset/schema/fields/filter/order/limits/identity。
现有 consumer 在完成逐 dataset 指纹迁移前仍按旧 manifest fail closed，不能直接
删除 catalog 校验。

TA 侧 canonical 实现为
`shared.governance.evidence_readiness.dataset_contract_fingerprint`。它保留
`default_fields/default_order/identity_fields` 的业务顺序，规范化无顺序语义的
filter operator 集合，并排除 state/degraded/runtime receipt 等运行元数据。调用方
必须从同一真实 catalog row 重算，不能接受 producer 自报 hash；任何上述七个合同
字段变化都形成新 fingerprint。不同仓若实现同一算法，须以跨仓 golden vectors
证明字节一致后才替换旧 catalog pin。当前首个 golden vector 位于
`tests/test_evidence_readiness.py`，其 SHA-256 为
`2a64eade6402119d492ae339213af96865ad5125358ac45de576b5a71f1d9e07`。

## 上游输入

### TradingDatas

`SHAREDSIGNALS_API_URL` 与 `shared.data.reader.TradingagentDataReader` 是待退役的旧源码/库接口，不是 current-v1 canonical client。兼容代码符号、schema ID 和文件名中仍可保留 `SharedSignalsV1*`，但不表示依赖旧 runtime。current-v1 的唯一数据边界是 TradingDatas V1 HTTP consumer：TradingAgent 不导入 TradingDatas 内部模块、不扫描兄弟仓目录、不读取其存储、不现场调用 provider，也不在读取失败时回退旧链、文件或本地拼装。

wire contract ID `sharedsignals.query_result.v1` 是产品重命名后保留的 immutable compatibility ID。机器状态仍把 upstream 与 TA consumer 分层记录：上游是否正式可用必须由 TradingDatas handoff 与 TA 自己的 readback 证明；本仓 repository contract、fixture 或 HTTP 200 均不能替代该证据。

V1 唯一路由为：

```http
GET /v1/catalog
POST /v1/query
```

`POST /v1/query` 请求是 provider-neutral 的显式契约；`schema_major` 必填：

```json
{
  "dataset_id": "explicit-configured-id",
  "schema_major": 2,
  "fields": ["field_a", "field_b"],
  "filters": {"trade_date": {"eq": "20260722"}},
  "as_of": "2026-07-16T09:25:00+08:00",
  "limit": 1000,
  "cursor": null
}
```

`order` 是可选的非空、有序且无重复字符串列表；未配置时从请求中省略，排序由 TradingDatas registry 默认值决定。`filters` 与 `as_of` 也按 dataset 显式配置：例如分区日线必须携带精确 `trade_date` filter；`query_as_of_mode=decision_as_of` 时发送决策时点，`query_as_of_mode=omit` 时不发送 `as_of`。TA 不得猜测默认排序、删除过滤条件或把一个 dataset 的查询方式复制给另一个 dataset。

`base_url`、`expected_catalog_version`、`dataset_ids`、`access_policy_id`、timeout 和 max limit 必须显式配置。`catalog_version_policy` 默认 `strict`；只有已用 canonical per-dataset fingerprint 验证目标 catalog rows 的消费者才可显式使用 `evidence_only`。后者仍要求先读取一次 catalog，并让随后每个 query envelope 的版本与本次观察一致；未读 catalog、读后版本再变或目标 dataset fingerprint 漂移都 fail closed。`access_policy_id` 只是 TA 本地 cache/receipt 对 transport 身份的命名空间，不是 credential。HTTP 认证只允许最终 transport 从 `TRADINGDATAS_API_TOKEN_FILE` 指向的仓外受限文件加载，再向两个固定端点注入 Bearer；通用 client、manifest、日志和调用方 header 都不得持有或覆盖 token。dataset ID 不允许从 provider 名称、URL 或返回行中猜测。响应 envelope 至少保留：

```yaml
api_version: v1
catalog_version: explicit-frozen-version
request_id: immutable-request-id
dataset_id: provider-neutral-dataset-id
data: []
metadata:
  state: observed_or_degraded_or_stale_or_failed
  degraded: false
  freshness: {}
  quality: {}
  lineage: {} # impaired state 可为 null
  receipt_id: immutable-receipt-id # impaired state 可为 null
  data_through: aware-time-or-version # impaired state 可为 null
  observed_at: aware-time # impaired state 可为 null
  reasons: []
```

HTTP 200 仅证明 transport 完成。每个 dataset 根据自己的 policy 独立 `ACCEPT / DEWEIGHT / REJECT`；`unobserved/paused/failed/stale/empty/degraded` 等 impaired state 可以如实携带 null `lineage/receipt_id/data_through/observed_at`，TA 不得补造。只有 `lineage.complete=true`、`lineage.provider_neutral=true`，且 envelope 的 `receipt_id/data_through/observed_at` 均完整时，才可形成 source proof；该 proof 绑定 dataset、catalog、receipt、完整 lineage hash、data-through 与 observed-at。无 source proof 的 dataset 固定 REJECT，不能因另一个 dataset 健康而放行。

Catalog 全 active-set parity 与 A股研究 profile 是两个不同对象。前者的
`tradingagent.tradingdatas.catalog-parity.v1` 回执只证明 manifest 冻结的 active
集合逐项完成固定 API、有界分页与同一 observation 双跑，并分别输出
`transport_contract_pass/ready_set_pass/impaired_set_accounted`。其中 ready
dataset 必须具备完整 source proof 并 `ACCEPT/weight=1.0`；预先声明的 impaired
dataset 必须 `REJECT/weight=0.0`，合法 null proof 只记为 unavailable accounting
事实，绝不进入 research snapshot。A股 observation profile 仍只消费其显式五项
角色；新增 active dataset 在完成业务映射和研究验证前不能因 catalog 激活而自动
进入 Universe、特征、候选或策略。

A股盘后 worker 的动态 manifest builder 每轮只调用一次 `GET /v1/catalog`，
把全部 active catalog rows 原样冻结到仓外、内容寻址、secret-free 的 catalog
snapshot，并从中校验三个固定业务角色：

```text
trade_calendar  -> cn.market.trade_calendar
security_master -> cn.equity.security_master
daily_bars      -> cn.equity.daily
```

这三个 dataset ID 是经过上游 catalog/handoff 审核的 TA 业务映射，不是由 provider
名称、alias、返回行或 URL 猜测。builder 只依据 catalog 的 `fields`、
`filter_operators`、`default_order`、`limits` 和 `schema_major` 生成查询合同；
完整分页交易日历用于确定最近完成开市日，证券主数据和该日 daily 再分别执行
`limit=1` 当前 metadata/source-proof 预检。三项任一不是
`ACCEPT/weight=1.0` 就不发布 current manifest。其它 active dataset 无论 catalog
runtime 为 success、partial、empty 或 degraded，都只进入 inventory snapshot，
固定 `research_auto_promotion=false`，不会被查询或自动加入研究。完整 observation
runner 随后仍须对三项执行有界终端分页、same-observation 双跑和五项 committed
binding，builder 预检不能替代它。

每个交易会话的 manifest 正文按内容寻址存入 `archive/`，稳定入口
`current.json` 只由同一私有 manifest root 内的原子 replace 发布。同一会话、同一
catalog/active contract 精确复用；同一会话 active contract 或核心 query contract
漂移时 `same_session_catalog_contract_changed` 并保留旧 current。新交易会话才
允许滚动 current。manifest、catalog snapshot 和 build receipt 都不包含 token、
Authorization、原始 cursor 或交易 authority；`historical_pit_eligible=false`、
`simulation_started=false`、`real_trading_enabled=false` 固定不变。

TradingDatas 返回的 `data[]` 是 **provider-native rows**，TA 原样保存，不把 envelope metadata 复制进每一行，也不生成虚假的 `available_time/revision_id/receipt_id`。dataset requirement 只声明：

- `identity_fields`：用于跨页唯一性与守恒检查；
- `observation_mode=current_observation`：当前唯一允许模式；
- `query_as_of_mode=decision_as_of|omit`；
- 可选 `row_event_time_field/format/timezone/semantic`：把 provider-native 的业务日期或时刻解释为 `session/scheduled/effective` domain event。

domain event-time 不是历史可知时间。`session` 事件不得晚于 envelope `observed_at` 或本轮 `decision_as_of`；`scheduled` 可以指向未来计划。只要上游不能提供当时首次可见时间与 revision 链，dataset 和完整 research snapshot 都必须标记 `historical_pit_eligible=false`，只能作为当前 observation、风险或模拟输入，不能回填历史训练样本。

token 文件合同固定为：只允许服务管理的 `/run/secrets/tradingagent` 根目录，必须使用该目录下的绝对且规范化路径；因此 TradingAgent checkout/worktree/Git common repo 或任意其它目录中的文件一律拒绝。路径任一层不得为 symlink，leaf 必须是可信 owner（root 或当前服务 euid）拥有的单硬链接普通文件、权限精确 `0600`、内容为单个有界 ASCII bearer token，不接受空文件、换行、`KEY=value`、非 ASCII 或超限内容。实现必须使用 no-follow、descriptor-relative 打开和读前/读后文件身份复核；平台缺少安全打开能力时 fail closed。只配置 token-file 路径，不接受明文 token 环境变量；token 值与路径不得进入 `repr/str`、异常、日志、manifest、回执或 fixture。Bearer transport 必须绑定无尾随斜杠、path、query、fragment、userinfo、控制字符或反斜杠的 canonical `scheme://host[:port]`，只允许 `GET /v1/catalog` 与 `POST /v1/query`，并只接受通用客户端固定生成的 `Accept: application/json` 与 POST 的 `Content-Type: application/json`；调用方自带 Host、forwarding、proxy 或任何其它 header 都必须在创建网络请求前拒绝。任何不同 authority、path、query string 或 method 同样拒绝；远端 authority 只允许 HTTPS，明文 HTTP 仅允许 loopback IP 字面量。transport 为 single-flight，并发第二请求在网络前拒绝；401/403 不读取响应正文并永久锁住本实例，后续请求、重试和端点切换全部拒绝。token 缺失/非法、认证失败、dataset impaired 或分页异常都不得回退到旧端口、SQLite、`/tushare`、`/source_status` 或 provider 专用 route。实际 TA-scoped token 仍由发布侧独立生成、注册和轮换，不能复用 TradingDatas bootstrap token。

#### TA integration-readiness profile 与回执

完整验收回执使用 `tradingagent.tradingdatas.integration-readiness.v2`。旧 `SharedSignalsIntegrationProbe*` 只保留为代码兼容符号。它不是 TradingDatas 服务端 receipt、生产健康证明或交易 authority。secret-free manifest 必须逐 dataset 绑定查询与研究合同，例如：

```yaml
manifest_version: 2
profile_id: explicit-profile-id
base_url: explicit-authority-url
catalog_version: explicit-frozen-version
access_policy_id: explicit-identity-not-a-secret
transport_id: http-json-v1
timeout_seconds: 10
as_of: timezone-aware-decision-time
expected_probe_roles: [trade_calendar, security_master, daily_bars, industry_context]
datasets:
  - probe_role: daily_bars
    dataset_id: provider-neutral-id
    schema_major: 2
    requirement_role: required_execution
    fields: [ts_code, trade_date, open, high, low, close, vol, amount]
    filters: {trade_date: {eq: "20260722"}}
    limit: 500
    minimum_row_count: 1
    identity_fields: [ts_code, trade_date]
    observation_mode: current_observation
    query_as_of_mode: decision_as_of
    row_event_time_field: trade_date
    row_event_time_format: yyyymmdd
    row_event_timezone: Asia/Shanghai
    row_event_time_semantic: session
    max_pages: 20
    max_rows: 10000
```

`expected_probe_roles` 与 `datasets[].probe_role` 的顺序和集合必须完全相等；dataset ID 唯一；`limit` 为 `1..10000` 且不得超过 `max_rows`；`identity_fields` 和可选 domain event field 必须包含在 `fields` 中。响应行保留 provider-native shape 并与显式 fields 精确投影；未知字段只以受控 reason/hash 报告，不写入回执。

完整 probe 对每个 dataset 做两次相同 observation read。每次从 `cursor=null` 开始，以 uncached 请求透明跟随 TradingDatas 返回的 opaque cursor，直到 terminal page；TA 不解析、记录或持久化 raw cursor。遍历同时受 dataset `max_pages/max_rows` 与代码 hard ceiling 双重约束，并执行：

- 跨页 envelope identity 全等：api/catalog/dataset 与完整 metadata 不漂移；
- cursor self-loop、A-B-A cycle、页数/行数超限立即 fail closed；
- `identity_fields` 跨页唯一，重复或缺失拒绝；
- 保留服务端原始页序与行序，不本地排序、去重或截断；
- 每页 query/response、cursor chain、完整 ordered rows、有序 identity sequence 与 metadata 形成 exact audit hash；非 terminal page 不进入 client cache。

两次完整遍历再比较排除 transport request IDs 与 opaque cursor 值的 semantic trace；metadata、receipt、lineage、data-through、observed-at、页结构、顺序或行值变化都会令兼容字段 `same_as_of_match=false`。cursor 只负责续页，服务端可在内容相同的两次遍历中返回不同 opaque 值；每次 exact trace 仍分别绑定自己的 request IDs、cursor-chain hash 与 cursor-bearing request hash，不能用 semantic hash 替代原始运行证据。这里的 “same-as-of” 是兼容术语：当 dataset 配置 `query_as_of_mode=omit` 时，实际证明的是同一 manifest/observation 双跑，而不是服务端历史 PIT。

回执至少包含：

```yaml
schema_id: tradingagent.tradingdatas.integration-readiness.v2
probe_version: 2
authority: non_authority
production_verified: false
real_trading_enabled: false
profile_id: explicit-profile-id
as_of: timezone-aware-decision-time
manifest_sha256: sha256
authority_sha256: sha256
catalog:
  request_id: trace-id
  catalog_sha256: sha256
datasets:
  - probe_role: daily_bars
    dataset_id: provider-neutral-id
    schema_major: 2
    query_sha256: sha256
    request_id_set_sha256: sha256
    state: ready
    evidence_action: accept
    receipt_id: source-receipt
    source_proof_complete: true
    page_count: 2
    row_count: 1234
    identity_sha256: sha256
    pagination_trace_sha256: sha256
    semantic_response_sha256: sha256
    same_as_of_match: true
    pagination_complete: true
    reason_codes: []
same_as_of_match: true
blocking: false
receipt_sha256: sha256
```

回执不得包含 base URL、access policy 原值、raw cursor、manifest 正文、HTTP header、credential、异常原文或上游自由文本 reasons。自由文本只进入 `evidence_reasons_sha256` 与完整响应语义哈希；对外 `reason_codes` 只保留 TA Evidence Gate 产生的受控代码。`receipt_sha256`覆盖除自身外的 canonical JSON，并绑定本次 request trace；完全相同的 trace 产生相同回执，新的 request ID 会形成新的精确回执。跨重试的业务一致性看 `semantic_snapshot_sha256` 与每个 dataset 的 `semantic_response_sha256`，它们排除 request ID。完整 integration probe / research snapshot 是 provider-native rows、source proof、分页、identity 与 current-observation eligibility 的权威消费侧门；轻量 runtime gate 只做启动前 catalog/auth/单次 dataset 可用性 smoke，不能替代跨页双跑、research snapshot 或历史 PIT 验收。

#### A股 Phase 1 current-observation binding

`shared.runtime.ashare_observation` 是完整 integration probe 之后的最小 A股观察绑定器，不是模拟订单或交易 authority。调用方必须显式提供仓外绝对 manifest、TA-scoped token-file 和 fresh state root，并固定 `REAL_TRADING_ENABLED=false`、`marketgraph_mode=mg_off`。首次绑定严格按以下顺序执行：

1. 加载并校验 secret-free v2 manifest；`daily_bars` 必须使用精确 `trade_date={"eq": "YYYYMMDD"}` filter。该日期必须由同一快照中的 `trade_calendar` 证明为不晚于 `decision_as_of` 的最新已完成开市日，不能简单等同 `decision_as_of` 的自然日；周末、节假日或延迟采集时允许 `observation_session < 上海本地 decision date`；
2. 对每个 dataset 做受 `max_pages/max_rows` 约束的双跑 integration probe，并要求 terminal cursor、跨页 identity 守恒和 same-observation semantic match；
3. 再读取一次完整 dataset set，且它的 semantic response、semantic pagination trace、identity、页数和行数必须与已通过 probe 的 observation 完全相同；
4. 证券范围必须同时绑定 `security_master` 与 `daily_bars`，并以两者 symbol 并集作为观察 denominator：master 固定请求 `ts_code/name/list_status/list_date` 且过滤 `list_status={eq:L}`；仅非 ST/退市风险、上市满 30 日、当日 `close>0`、`vol>0` 且 `amount>0` 的沪深主板普通股进入 `observation_universe`；主数据中有效但当日日线缺失、日线孤儿、停牌/零成交和非主板个股都必须以稳定 reason code 显式记录，不得从 denominator 静默消失；
5. 仅在以上门禁通过后，先持久化不可覆盖的 transaction intent，再依次冻结 integration probe receipt、`ResearchDataSnapshot`、aggregate observation receipt 与逐股 membership ledger；四项内容全部精确读回后才写 transaction-complete commit marker；
6. 新契约的可消费权威是“四项数据证据 + 一项 commit proof”的五项绑定。精确重放必须同时验证五项内容、身份与完成标记，且不再次创建 transport 或联网。没有 intent/complete marker、membership ledger 或任一精确内容的旧/半写状态必须显式阻断；崩溃恢复只允许在 session 锁内修复同 inode、同 owner、`0600`、canonical exact payload 的唯一 publish 临时硬链接，不得从旧 receipt 或今日数据反推、补写或回填。

下游不得用调用方传入的 mapping、hash 或直接构造 dataclass 自授 observation eligibility。唯一可晋级入口是 `load_verified_ashare_runtime_authority_bundle`：它只从同一 `0700`、当前服务身份所有的 state root，在 writer 的 session lock 内读取并重验 snapshot、probe receipt、observation receipt、membership ledger 与 transaction-complete marker；公共 `build_ashare_runtime_authority_bundle` 仅作关闭式合同诊断，永远附带 `verified_observation_state_required`，不能生成 eligible bundle。history、planner 与日线估值 adapter 只接受 loader 产生的 verified typed bundle；日线 mark 的 source lineage 还必须组合绑定 membership 与 complete hash。

观察结果 schema 为 `tradingagent.ashare.current-observation.v1`，至少包含 `snapshot_sha256`、`probe_receipt_sha256`、`observation_receipt_sha256`、`observation_ledger_sha256`、`observation_transaction_complete_sha256`、`profile_id/catalog_version/decision_as_of`、`observation_session`、`observation_universe_count/hash`、排除原因计数、`context_probe_roles` 和 `idempotent_replay`。结果固定：

```text
mode=observation_only
marketgraph_mode=mg_off
real_trading_enabled=false
historical_pit_eligible=false
execution_authority=false
```

`daily_bars` 原始快照可以保留全市场 provider-native rows，以免环境样本只剩主板；每行 `trade_date` 必须与交易日历确认的 `observation_session` 完全一致。envelope `data_through` 的上海本地日期也必须等于该 session，但不要求伪造为 15:00；`observed_at <= decision_as_of`，且 freshness/quality/lineage/receipt 仍逐数据集 fail closed。membership ledger 允许 session 早于 decision 自然日，但绝不允许 session 晚于 decision。`observation_universe` 只是观察初筛，不是 Account Tradable Universe、Small-Capital Feasible Universe、候选、仓位或订单池；观察资格不授予任何资金或执行 authority。membership ledger 中的 excluded row 只作 denominator/排除法证，不得进入 Feature、Candidate、Forecast、Position、Order、LLM 或任意外部逐股输出。

旧 receipt 中的 `tradable_universe_count/hash` 仅是限时兼容别名，其语义与 `observation_universe_count/hash` 相同，绝不表示 broker permission 或订单资格。新 writer/reader 不得再使用该别名作 authority；它仅为旧回执读回保留，待旧消费者和服务器状态根完成 parity 与清零证据后退役。

创业板、科创板和北交所个股只进入受控排除计数，绝不能进入候选、仓位或订单。`optional_context` probe role 只接受关闭集合 `industry_classification/industry_daily_context/industry_context/index_context/market_breadth/sector_context`；任意个股候选、子串伪装或不明 role 均 fail closed。当前 `index_classify` 与 `sw_daily` 只能证明行业分类/行业指数环境观测，没有成分股 denominator 与 coverage authority 时不得称为完整行业宽度。观察绑定不导入或调用 capital、portfolio、order、outbox、reconcile、broker 或 SampleJournal writer，也不存在数据库、旧 route 或 provider fallback。

`as_of`、domain event-time 和 envelope `observed_at` 都不是防止回填偏差的充分条件。任何进入 predictive validation 的 dataset 仍须由独立上游证据提供首次可见 `available_at`、release/revision 链、first-seen receipt 和训练时 vintage；缺这些历史事实时一律保持 `current_observation` 与 `historical_pit_eligible=false`。

#### A股 forward-observation history 与 paper-planning 停止线

每个 session 的主板 history 必须逐日绑定上述五项已提交证据，且 target symbol 必须在当日 membership ledger 中为 `observed/phase1_mainboard_observed`。缺 transaction-complete hash 的四项半写状态不是历史样本。输入顺序就是收集顺序；不得重排、修复、补日、从今日 master 回填历史成员，也不得把 `current_observation` 升级为历史 PIT。`momentum_20d` 与 20 日波动需要至少 21 个 forward-collected session 才具备最小数学覆盖；但在独立交易日连续性 authority 和公司行动/复权 authority 缺失时，`prospective_history_eligible`、各数值 feature readiness 与预测资格仍必须为 false，不能只因 session count 达到 21 而解锁。

Phase 1 membership ledger 当前 `label_horizons=[]`，`learning_eligible=false`。盘后 T 日日线没有 T 日分钟锚点，也没有自动获得 T+1 交易会话；因此不得从日线自动生成 `m30/m60/close/1d/3d/5d` 标签请求。只有未来冻结且独立验证的 calendar/minute/market-truth/adjustment authority 才能为新样本预先注册目标会话；不得反向补标历史 ledger。

当前 daily-only paper planner 固定输出 `status=completed_with_blocks`、`action=abstain`、`authority=non_authority`、`paper_trade_session=null`，其 decision/artifact/day binding 必须继续绑定 transaction-complete hash，并至少保留 `next_trade_session_authority_unavailable`、`champion_numeric_features_unavailable` 和 `minute_execution_evidence_unavailable` blockers。T 日收盘 observation 只能在预测前冻结的交易日历证明下映射到 T+1；在此之前绝不生成 capital/reservation/order/fill/outbox/reconcile/SampleJournal 副作用，也不得把日线 close 冒充成分钟/L1 价格或可成交证据。

### A股三层 Universe 契约

`tradingagent.universe_scope.v1` 生成三个不可变快照：

该 scope 的运行 port 固定为 `CanonicalMainboardScopePolicy`；其 stage-neutral component
identity 绑定冻结 manifest SHA-256。composition 和离线 fixture parser 都必须验证精确类型
及 identity 内容，不能接受调用方自报的等价 callable、派生类或任意 artifact digest。

- `MarketContextUniverseSnapshot`：可包含全 A 指数、创业板指、科创 50 和全市场行业聚合，且必须 `context_only=true`；其 `coverage_scope` 只能由内容寻址 `tradingagent.market_context_coverage.v1` receipt 及本次实际聚合对象派生，调用者不能自报 `full_market`；
- `AccountTradableUniverseSnapshot`：当前历史类型名只代表 `simulation_only` 主板scope policy，明确 `broker_permission_status=unverified`；未来订单链还需另行绑定真实账户权限和 PIT 证券主数据；
- `SmallCapitalFeasibleUniverseSnapshot`：当前候选只叠加不超过 50,000 CNY 的模拟cash+policy upper bound、100 股整手、单票 15%、费用/滑点、流动性和最小经济订单；`single_name_max_pct`、`minimum_economic_order_cny`、`max_adv_participation_pct`、`lot_size_shares` 进入不可变hash并对非法值fail closed。`position_state_applied=false` 时的 `max_buyable_shares` 不是订单量；真实持仓、可卖数量、reservation和ledger proof必须在组合/风险/OMS前另行验证。

后层只能增加约束。非主板个股在 Feature、Candidate、Forecast、TargetPosition、TradeIntent、ShadowBook、Order、Fill 和 Position 必须为零；CoverageReceipt 必须绑定 taxonomy/version、membership effective/available/valid time、board/sector expected/observed denominator、coverage ratio、source generation/receipt/lineage 和 source/content hash，并要求实际行业聚合 ID 集合与 receipt 逐项一致。receipt 缺失或非法直接拒绝；过期、数量异常、taxonomy/板块/行业缺口或本次双创聚合缺口只能输出 `partial_market + degraded`，不得用主板子集伪装全市场。精确 reason code 和快照 hash 见 [universe_contract.md](universe_contract.md)。

Universe历史验证还必须绑定PIT证券主数据：上市/退市、板块迁移、ST/风险警示、停复牌，以及历史指数与行业成员的effective/available time。CoverageReceipt证明某次decision time的denominator与观察覆盖，不单独证明历史样本已消除幸存者偏差；缺任一历史状态链时不得把今天仍存续的证券集合回填为过去Universe。

Coverage denominator 不能由调用方或 receipt 自证。`CoverageAuthorityVerifier` 是注入式信任边界，TradingAgent 没有默认实现；它必须返回 `CoverageAuthorityVerification`，精确绑定 source generation/receipt/content SHA、taxonomy ID/version/sector count、`assessed_as_of`、verifier/proof identity 和验证时点。构建与消费快照都会重新验证这份 proof。verifier 缺失、拒绝、绑定漂移、未来验证或 proof 篡改时，只能拒绝或降级，不能发布 `full_market`。

### Phase 1.5 行业 shadow authority

`tradingagent.industry_shadow_basket.v2` 只选择 1 个深研行业和 2 个观察行业。每个 `IndustryShadowInput` 至少绑定：

- taxonomy、PIT membership、expected/observed member count 与 coverage ratio；
- `activity_score` 及 `score_method_id/version`；
- `score_observed_at <= score_available_at < score_valid_until`；
- score receipt ID/SHA、coverage authority receipt ID/SHA、evidence receipt IDs；
- canonical `score_content_sha256` 与 source generation。

调用方必须注入没有默认实现的 `IndustryScoreAuthorityVerifier`。返回的 `IndustryScoreAuthorityVerification` 必须逐项绑定 decision time、industry、score content、score/coverage receipts、verifier/proof identity；未来、过期、缺失、篡改或拒绝均 fail closed。basket hash 同时绑定所选三项及其 proof。该对象固定 `shadow_only=true`、`context_only=true`、`position_effect_allowed=false`、`promotion_eligible=false`，不含 symbol，也不能影响 Champion、排序、仓位、风险或订单。当前只有 fixture verifier；真实评分和覆盖 authority 未接入前，不得称为 live 行业智能。

### LLM 证据 sidecar（本地候选）

密钥变量名固定为`DEEPSEEK_API_KEY`且公开配置对象不读取其值；其它格式合法的环境变量名也拒绝。任意模型映射只能生成`fixture_only`离线路由，不能作为provider egress授权；独立的宽松环境变量路由旁路已移除。validated router只有在调用方同时传入`allow_network_transport=True`时才能从network环境标志形成候选；该标志本身不读取credential、不执行网络，也不能替代精确HTTP transport注入。

`EvidenceArtifact` 的 source span 是 `untrusted_artifact_data`，不是Prompt指令。请求必须绑定固定template/version、artifact content hash、source authority proof、PIT cutoff和完整artifact-set hash；proof至少保存verifier ID/version、verified time、source receipt与proof hash。transport前先做全树敏感数据门，再对source span中的显式角色覆盖、忽略既有规则、系统/开发者指令注入等中英文模式做负例门；安全扫描会额外规范化NFKC/NFKD、零宽/控制/组合字符、HTML entity、最多三层URL编码、定向JSON Unicode escape、部分常见同形字及高置信compact skeleton。命中后禁止transport、保留引用并输出human-review reason；这仍是启发式门，不代表完整语义安全。

A股Prompt registry保留`bull-bear-evidence.v1`的字节冻结历史，当前A股request builder使用`bull-bear-evidence.v2`。v2的provider wire output必须是不带Markdown、代码围栏、前后缀或解释的原始JSON对象，且恰好包含`bull_case`、`bear_case`、`key_risk`、`contradictions`、`material_facts`、`evidence_refs`和`confidence_note`七个字段。前三项为非空字符串；两个事实/矛盾字段为字符串数组，无内容时显式输出`[]`；`evidence_refs`为非空字符串数组，每项必须逐字复制自本次`untrusted_artifact_data[].artifact_id`；`confidence_note`为字符串。额外字段、决策字段、未知/改写引用、类型错误或空核心字段均fail closed。v2已经离线fixture合同验证，但未进行第二次真实provider canary，不得从本v1失败推断v2已修复真实输出。

当前只允许两个精确transport类型：`OfflineDeepSeekFixtureTransport`的不可调用冻结响应，或默认关闭的`DeepSeekHTTPTransport`。普通callable、类型子类和未知request/outbound identity都在副作用前拒绝；HTTP transport公开`send`与脱离Gateway的Adapter调用也固定拒绝，内部wire path只接受Gateway完成request/source-proof/Prompt注入/DLP验证后铸造的exact-type capability，该对象以进程内HMAC绑定body、批准模型、request hash、source-authority proof-set hash、transport material hash和outbound hash。fixture身份同时绑定request与outbound，HTTP路径还必须使用validated DeepSeek V4 router、固定官方HTTPS endpoint和显式raw-secret文件。默认Gateway通过严格、无密钥配置构造。2026-07-18一次隔离的`deepseek-v4-flash`请求已到达HTTP 200 provider envelope，但A股v1输出在evidence binding阶段失败；这是失败闭合证据，不是可用的认证readback。

offline receipt必须使用固定offline transport ID/version/policy，不能声明HTTPS身份。HTTP路径还必须在router中携带不可变的`network_authorized=true`；仅注入已启用HTTP transport不能覆盖provider网络门。默认Gateway的router为`network_authorized=false`，两道门任一缺失都必须在读密钥或网络副作用前fail closed。

`ProviderTransportReceipt`只表示provider envelope、evidence schema/引用绑定与Gateway observation均成功。它绑定provider/model、transport ID/version、request hash、包含本地source-authority proof的`transport_material_sha256`、provider outbound的`outbound_sha256`、response hash、标准化evidence hash、provider response ID、verified/received time与receipt hash。`transport_metadata`是receipt identity的一部分，精确包含`kind`、`endpoint`、`method`、`egress_policy_version`、`http_status`、`content_type`、`request_bytes`、`response_bytes`、`attempt_count`和`retry_disposition`。HTTPS只允许DeepSeek两个批准模型、官方transport ID/version/policy、固定endpoint、`POST`、HTTP 200、`application/json`、正请求/响应字节数、attempt 1和`not_retried`；离线fixture使用独立固定元数据。HTTPS的`response_sha256`按原始HTTP body bytes计算，不对解析后对象重新序列化。

`ProviderRejectedAttemptReceipt`是与成功receipt完全独立、互斥的审计合同。它只能在精确`DeepSeekHTTPTransport`已验证HTTP 200、MIME/JSON和provider envelope，但后续`provider_evidence_validation`或`gateway_observation_binding`失败时由内部transport capability铸造。固定`outcome=rejected`、`reason_code=llm_evidence_schema_invalid`、`evidence_accepted=false`、`evidence_journal_eligible=false`、`production_eligible=false`、`audit_only=true`，六项authority全为false；仅保留安全transport元数据与request/source/material/outbound/raw-response hash，不保留provider正文、parsed/normalized evidence、`normalized_evidence_sha256`、provider response ID、credential或credential fingerprint。它不适用于offline fixture、非200、坏MIME/JSON/envelope、前置权限/DLP拒绝或敏感输出，不得创建`LLMEvidenceEnvelope`、写入accepted `LLMEvidenceJournal`、进入样本/成熟度/晋级或给予任何候选、风险或交易权限。它只可通过完整`GatewayAnalysisResult`和显式`LLMEvidenceProvenanceRecorder`写入物理分离的`LLMRejectedAttemptAuditJournal`；readback为mapping-only，绝不重建typed/capability receipt。它是本地内容寻址审计事实，不是provider attestation、外部签名或production durable authority。`GatewayAnalysisResult`只能由Gateway内部能力强制两类回执互斥并精确绑定canonical observation字段集、原request/entity/prompt/refs、provider/model/status/reason/output hash和重新计算的request/source/material摘要；额外字段、缺字段、元数据重绑或正文hash漂移均fail closed。Adapter没有外部receipt sink，调用方取得完整`analyze_with_provenance()`结果后也只能交给上述typed recorder路由，不得自行发明第三种或混合持久化协议。

非200、非法MIME/编码/JSON/envelope、敏感输出或引用绑定失败均不产生accepted receipt；其中只有上述两个真实HTTP schema/binding阶段可产生审计用rejected-attempt receipt。内部proof/审计metadata只参与本地内容绑定，不进入outbound；JSON mode、thinking、`reasoning_effort`与bulk/pro 4,096/8,192 token上限仍只是本候选请求构造合同，不是当前账户接受证明。2026-07-18一次真实Flash请求得到schema-rejected结果且原始响应未持久化；它不能被本轮后新增的typed rejected receipt追溯包装，也不证明生产verifier或模型可用。

成功且完整验证的offline或HTTPS结果可以封装为`tradingagent.llm_evidence_envelope.v1`并写入显式绝对路径的`LLMEvidenceJournal`；schema-rejected HTTPS结果只写入由该accepted锚点确定性派生的`LLMRejectedAttemptAuditJournal`；provider调用仲裁只写同一canonical family内的`LLMProviderInvocationJournal`。`llm_provenance_journal_paths()`是唯一伴随路径推导合同；另配invocation锁、相对路径或不匹配的Journal family一律拒绝。三条Journal及其`.head`共六个端点必须全部互异且没有默认runtime位置，路径在构造时冻结为绝对路径，并按Unicode NFC、大小写、真实路径与已存在device/inode去重。rejected事件schema固定为`tradingagent.llm_rejected_attempt_audit_event.v1`，其`attempt_id/event_id`由typed receipt SHA确定性派生，并绑定canonical invalid observation、observation hash、occurred time、完整脱敏receipt descriptor、previous-event hash与event hash；它只表达`local-integrity-only`审计，不具evidence eligibility。invocation事件schema固定为`tradingagent.llm_provider_invocation_event.v1`：不依赖调用方request ID的逻辑内容键同时绑定request schema、entity、route、provider/model、Prompt、PIT cutoff、artifact set和payload；网络前先持久化`in_flight`，并在同一跨进程文件锁内完成双结果Journal检查、provider调用与唯一`accepted/rejected/no_receipt`终态。`LLMEvidenceProvenanceRecorder`要求三条精确typed journals、与DeepSeek Adapter同对象的source verifier和精确`LLMEvidenceGateway`；Bull/Bear provider模式还要求调用方提供稳定、受限格式的request ID。已持久化终态的同ID同内容顺序或并发重放不再次调用provider；同一canonical family内相同逻辑内容换ID、同ID异内容、双重结果或端点别名fail closed。provider调用后崩溃且没有对应可验证结果时保留`in_flight`，后续不得自动补发；若唯一accepted/rejected结果已成功落盘但invocation终态未写完，可在同一锁内重绑receipt和observation后补齐终态。只有互斥结果对应的唯一Journal与invocation终态写入成功或精确幂等后才返回观察，任何read/append/CAS/身份校验失败均不得返回available。`run_id`由当次typed transport receipt确定性派生；accepted envelope同时绑定请求、outbound、response hash/标准化evidence、source authority proof、artifact set、provider receipt和观察结果。三类readback都对descriptor做完整结构/hash校验并保存防御性不可变快照，只返回非权威mapping视图，绝不重建运行时typed receipt。Journal采用append-only canonical JSONL、事件checksum chain、expected-head CAS、状态迁移/同run/attempt幂等、冲突拒绝和本地`.head`锚点；Journal与head在持锁读写期间都要求regular file、`st_nlink=1`、owner为当前euid、mode精确`0600`，且`stat(path)`与打开FD的device/inode一致，读写后再次复核。该journal和head只能发现本地断链、交换、截断或单侧篡改；它们不是外部签名、远程密封、production durable receipt authority，也不能抵抗journal与head被同时替换或删除。模式门不能被描述成覆盖全部语义型、混淆型或编码型prompt injection。任何LLM输出或journal记录都无候选成员、排名、概率、仓位、风险豁免、订单和账户authority。

输入包括 assets、日线/5 分钟行情、交易日历、合约元数据、基本面/因子/资金流/宏观/情绪、事件、行业 taxonomy/membership/snapshot 以及数据覆盖/新鲜度/source status。

行情是证据，不是交易信号。数据不可用、陈旧、缺来源或 PIT 不完整时，新增风险 fail closed；observation 仍保存并明确 data-quality/label eligibility。市场治理隔离：无关市场故障不能误停 A股或 CNFutures。

每次真实 TradingDatas HTTP response 都在 cache 前保存独立 `sharedsignals_response_lineage`，至少含 `transport=http_response`、endpoint 与带时区 `received_at`。该字段是产品重命名后继续保留的 immutable wire ID。provider 自带 `evidence_envelope` 或其中任一 group 结构非法时，原非法值必须原样保留供 Evidence Gate 拒绝；transport lineage 只能作为本次网络响应审计，不能覆盖、修复或洗白 provider lineage。cache 命中必须返回同一审计事实且不能再次发起 HTTP。

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

## 按市场隔离的 capital contract

本节的 append-only capital ledger 字段只规范 A股与 CNFutures 两套 50,000 CNY
simulated authority。Crypto 的 10,000 USDT 数值只来自 `Crypto/capital_policy.py`，是
本地 fixture opening candidate，不是 current、runtime、execution 或 durable capital
authority；`Crypto/config.yaml` 只允许 simulated fixture 写入，既有 shadow artifacts 只读。
Crypto 金额字段仍必须显式携带 `market=crypto,currency=USDT`，不得套用 `_cny` 字段、
固定汇率或国内 ledger authority。三个市场在 All Markets 层均禁止货币聚合。

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

CNFutures 使用相同初始权益和 generation，并以 `margin_utilization_limit_pct=0.50` 取代 A股单票/gross 字段。两套国内 policy 均为 `fresh_start_approved`，不接受 cross-market allocations 或 fixed protected cash；Crypto 的 10,000 USDT 本地 fixture opening candidate 不参与这两套 policy，也不是第三套 current capital snapshot authority。

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

A股可卖数量只从 append-only `fill_commit` / `ashare_sell_commit` 重放：唯一公共批量投影为 `ashare_sellable_quantities(trade_date) -> dict[symbol, int]`，按 Asia/Shanghai 的 timezone-aware `filled_at` 建立买入批次，卖出只 FIFO 消耗早于卖出本地交易日的批次，并为每个当前持仓 symbol 显式返回可卖数量（包括 0）。投影必须与同轮 ledger replay 的 `latest_position_quantity` 全等；时间倒退、查询日期早于既有成交、非法日期/数量/方向或持仓不一致均 fail closed。`commit_ashare_sell` 在追加事件前和候选事件重放后都执行该校验，不信任订单或行情快照自报的 `sellable_quantity`。

partial 只结算 actual quantity；terminal 同一事件释放未使用 reservation。commit 成功或幂等成功前，fill 不得计入 execution-eligible 策略绩效。outbox pending 必须对新增风险保守可见。pending intent保存内容绑定的完整receipt seed；恢复时只有在canonical ledger独立返回同一commit的幂等事实后才可先补settlement，只有intent或CAS已变化但无commit事实时不得绕过最新risk/drift授权。

capital-backed risk wrapper 的输入必须是尚未携带任何当前或legacy预约字段的原始订单，避免重复预约或继承他单预约。只有`open/increase`可以由该wrapper生成并携带`market_capital_reference_id/reservation_id/reservation_event_id/lineage/reserved_cash/reserved_exposure/required`；execution对买单继续拒绝全部legacy别名，`reduce/exit`在risk与execution两层拒绝任何预约字段，且`release_unfilled`永不为卖单释放预约。买入零成交释放前会按同一run/order构造canonical reservation reference，由capital ledger复核reservation event、authority、generation、execution lineage、risk unit与lineage，并要求订单中的reserved cash/exposure精确等于canonical当前完整剩余值；不能由订单把释放额缩小为部分清理。新release必须服从effect guard，随后精确event在该事件时点就必须得到`terminal=true`且remaining cash/exposure/margin全零；同一reference的重放只接受canonical已存在的相同终态事实。回执保存完整预约证明。reconcile不能只接受非空release ID或事后变成零的最终状态，而必须重放到精确release event，验证同一reservation、金额、原因、reference及event ID，并证明该事件当时已终态全额释放。

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

### 小资金订单计划绑定（本地候选）

三层Universe中的`SmallCapitalFeasible`仍只是`cash+policy upper bound`。进入模拟决策链时必须另生成精确schema的`tradingagent.small_account_plan_receipt.v1`，至少绑定：

- 当前模拟capital authority、正整数generation、`account_state_as_of`与50,000 CNY risk base；
- position receipt ID/SHA、完整account content SHA、detached verification receipt、verifier identity/version/有效期、当前持仓/mark和每只股票T+1 `sellable_quantity`；
- 买入100股整数倍及卖出零股/全退例外、15%单票上限、90% gross上限、current/target gross、starting/ending cash；
- 当前 `cost_policy_id`，以及每项plan decision的symbol、side、quantity、reservation price、fee、cash before/after和reason；
- 每个新仓候选的不可变 score evidence：canonical 路径必须是绑定当前人工选择 Champion manifest、artifact SHA、model ID/version、精确冻结 spec、symbol、PIT decision time，以及经独立port复核的数值PIT特征快照/数据receipt/vintage/lineage/known time/实现SHA/归一化版本/source type、computed rank 和 receipt hash 的 `ChampionScoreReceipt`；fixture 路径只能使用明确 `offline_engineering_fixture_rank`、不可晋级的独立 evidence 类型；
- canonical plan digest与生成时点。

Optimizer必须调用无默认实现的`AccountAuthorityVerifier`；proof逐项绑定authority/generation/source、account as-of、现金/gross、positions/sellable/mark、position receipt/hash、完整account content hash与有效期。future、expired、篡改或不匹配均fail closed；offline fixture proof固定不可晋级。调用方不能直接注入 raw `rank_score`。canonical decision 必须重新调用`ChampionSelectionVerifier`和`NumericPITFeatureSnapshotVerifier`，并复算完整score receipt；过期/非当前Champion、future/LLM/过早feature proof、调用方自证或fixture evidence冒充canonical authority均fail closed。未校准 rank 只用于候选排序，禁止通过 `single_name_max * rank` 或其它映射控制目标金额。Phase 1 新仓统一使用与 rank 无关、受最低经济金额、整手、费用、现金、15%单票、90% gross和最多8仓约束的固定 probe sizing，并明确标注为 engineering simulation。fixture路径只证明输入/proof内容绑定；canonical-capital测试路径从同一simulated ledger head派生并复读账户，current generation/lineage只能来自该snapshot而不能比较历史常量。Day loop再独立重算计划数值和hash，不相信stage自报。费用按canonical佣金、过户费和卖出印花税逐项复算，重新签名不能洗白错费用。Risk及每个approved order必须匹配plan SHA、position receipt、symbol/side/quantity/reservation price/fee并顺序重算现金。买入须为100股整数倍；卖出仅允许100股整数倍、一次性卖出当前不足100股余额，或全部退出，且不得超过可卖数量。optimizer、day loop与sim engine任一处不满足都在模拟成交前fail closed。两条本地路径都不证明真实账户、生产Champion/feature registry或broker状态。

### 六维投资论点风险 authority（本地候选）

每次调用optimizer必须显式提供不可变`ThesisRiskRuntimeAuthority`；没有默认policy、默认verifier或runtime自签路径。authority至少包含：

- 人工复核且有有效期的`ThesisRiskPolicy`，六个维度固定为`industry / thesis / raw_material / policy_event / crowding / model_family`，每个cap为有限非负CNY金额；
- 每个候选、非零现仓及所有未终结open/increase pending预约的`ThesisRiskExposureReceipt`，绑定symbol、exposure kind、六维group、有限非负notional、PIT时点、source receipt/lineage/content hash和对应candidate/position/pending事实；pending reduce/exit不重复增加风险；
- 每个receipt的独立detached verification proof，以及一个覆盖全部成员、不可漏记/增补/重复的`ThesisRiskExposureSetReceipt`和set verification proof；proof必须在decision time有效并明确`promotion_eligible=false`时只能用于fixture；
- policy、proof、set、初始六维exposure map和authority context的内容hash。

Optimizer按当前持仓与pending book计算pre-exposure，再逐动作应用精确notional delta；同一symbol的candidate、position与open/increase pending必须保持六维group一致，不能靠重分类规避cap。open/increase超cap只可拒绝该新增风险，reduce/exit不得因cap已超而被锁死。Day loop不相信plan自报：它把每个决策的六维`group_id`重新绑定回权威exposure receipt，并验证authority context、逐订单金额变化、pre→post group连续性、最终六维map和plan hash；对payload改值后重新签名、遗漏pending、跨决策把pre exposure归零、替换policy/proof/set、改换group或过期proof均fail closed。外层stage为不可晋级不能掩盖任一嵌套proof的`promotion_eligible=true`。当前fixture policy及verifier只证明本地合同完整性，不证明生产行业/论点映射、真实订单预约、上限科学性或外部签名。

Canonical A股账户估值 mark 不接受“只要早于决策时刻即可”的宽松口径。每只实际持仓的 mark 必须嵌入精确`AShareMarkEvidence + MarketEvidenceVerification`，绑定dataset/catalog、source receipt ID/SHA、source lineage、price payload、`data_through/observed_at/available_at`、`market_session=close`、完整`session_calendar_receipt`以及capital authority/generation/execution lineage/run decision context。按该 calendar，mark 的 `trade_date` 必须是运行日最近前一交易会话，`data_through=observed_at` 必须为该会话上海时间 `15:00:00`，且 `available_at <= account_as_of`。调用方另传的 `mark_observed_at` 必须与账户内不可变 evidence 完全相等；canonical position receipt 继续绑定由 mark evidence 参与形成的 ledger event/checksum、market authority SHA与verification SHA。当前唯一具体verifier是不可继承的`non_production_fixture`、`production_eligible=false`类型；proof hash只证明本地内容完整性，不是签名、生产calendar、TradingDatas live receipt readback或市场行情authority。

## A股机会、预测与三风格 shadow 合同

### OpportunityRadar 与 Ledger

`FrozenOpportunityRadar`对显式`scanned_entity_ids`全分母扫描，并要求无默认实现的`OpportunityCoverageVerifier`返回detached proof。proof必须精确绑定detector ID/version、decision time、Universe snapshot、scan rows、entity ID集合及expected/observed count；缺失、拒绝、future、数量不等、内容漂移或`production_eligible!=false`均fail closed。股票scope只允许沪深主板普通股；市场/行业scope只能是`context_only`聚合。

每个`OpportunitySnapshot`保存`latent / forming / ready / triggered / active / decaying / invalidated`状态、未校准hazard score、priced-in score、触发窗口、horizon、thesis、失效条件和逐条PIT evidence receipt/lineage/hash/expiry。状态迁移只能沿冻结状态机进行，必须增加新的、在decision time已可知且仍fresh的证据，并绑定previous snapshot SHA；`invalidated`为终态。`OpportunityLedger`只写`scan_batch`与`state_transition`两类append-only事件，采用canonical JSONL、checksum chain、expected-head CAS和内容相同幂等；它不是订单队列。Batch、snapshot与ledger固定`shadow_only=true`、`candidate_emission_allowed=false`、`position_effect_allowed=false`、`order_effect_allowed=false`、`promotion_eligible=false`。

### 多期限 forecast

`MultiHorizonForecastSnapshot`只接受现有标签期限`m30/m60/close/1d/3d/5d`，逐期限保存单调`q10/q25/q50/q75/q90`和`score_semantics=uncalibrated_return_quantiles`；未校准时`positive_probability`与`outperform_probability`必须为null。可选event hazard必须保存事件定义/version、窗口、censoring和competing-risk policy，且仍为未校准score。snapshot逐项绑定Opportunity、research snapshot、model release manifest、ValidationPlan和frozen OOS receipt，生成时点必须等于Opportunity decision time，并固定shadow-only、无仓位/订单/晋级authority。

概率只能存在于分离的`CalibratedForecastResearchArtifact`。它要求无默认实现的`CalibrationAuthorityVerifier`复核exact forecast/probability set/ValidationPlan/OOS receipt、有效期、Brier、Log Loss、ECE和至少40个有效独立样本；proof仍固定`production_eligible=false`。这个artifact只证明本地冻结研究合同，不能回写原forecast、不能发布为live概率，也不能进入Champion或订单链。

### 三风格 shadow router

当前风格固定为`industry_trend / event_surprise / cross_market_dislocation`。每个`StyleSleeveReceipt`只能对同一个主板Champion candidate给出`support / oppose / abstain`和未校准heuristic score，并绑定base snapshot、Champion score receipt、decision time、版本、reason codes及不可变evidence groups。相同evidence group必须共用同一source receipt SHA，router按group去重，禁止用多个名称重复计算同一证据。

`StyleRouterRunReceipt`只输出反事实`open_candidate`或`abstain`。支持与反对同时存在时必须abstain；支持意见可排序primary/supporting style，但票数和raw score不能改变50k资金、仓位或风险。run固定`router_mode=shadow_only`、`decision_eligible=false`、`position_effect_allowed=false`、`order_effect_allowed=false`、`automatic_promotion_enabled=false`、`automatic_risk_expansion_enabled=false`和`live_transition_authorized=false`。现役订单链不得导入`shared.opportunity`、`shared.forecast`或`shared.strategy_router`。

## 旧 A股 prediction 与 exploration（冻结兼容）

以下字段只描述旧四风格/探索样本的只读审计合同，不是V1现役router。每个旧风格 prediction 保存：

- `style_id`、`style_version`、lifecycle/hypothesis family；
- entry/exit thesis、holding horizon、direction；
- `raw_style_score`、`score_semantics=uncalibrated_heuristic`；
- `calibrated_probability=null`、`probability_model_state=not_calibrated`；
- `uncalibrated_return_prior` 与 model state；
- risk request、abstain/reject reason；
- capital authority scope、prediction snapshot/base snapshot SHA、MarketGraph ablation；
- forward-label request。

不得输出无校准证据的 `probability` 或把 prior 当作未来收益预测。

旧Exploration selection 保存：`exploration_policy_version`、top-K/pool、seed、selection method、`selection_probability`、`propensity`、chosen symbol/style/snapshot 和 not-selected reason。probability/propensity 必须相等且在 `(0,1]`。V1不得新增这类selection、不得把它恢复到当前KPI或订单链。

历史订单归因可保存 `sample_intent`、`primary_style`、`supporting_styles`、`style_scores`、`style_versions`、`decision_policy_version`、disagreement 和 exploration metadata；它们必须标记legacy/excluded。风格从不拥有 capital account。

## A股本地执行事实

- 执行 authority 路径由 verified current capital snapshot 的 `execution_lineage_id` 派生为
  `shared/logs/execution_lineages/<execution_lineage_id>/`；同 root 的 positions、trades、manifest、
  receipts 与 outbox 必须匹配同一 authority/generation/lineage。历史
  `ashare-sim-fresh-20260712-v1` 只是某次基线 lineage，不是 reader 常量；旧
  `shared/logs/local_sim/` 冻结且不参与当前统计。
- 签名回执：同一 fresh execution root 下的 `sim_execution_receipts.jsonl`；不得回退读取旧 `signals/` 或 `shared/logs/local_sim/` 回执作为当前 authority。
- 同日幂等键至少包含 market + account + trade date + symbol + side；多风格不得重复下单。
- execution-eligible fill 包含 actual positive price/quantity、timezone-aware timestamp、真实 5 分钟 source/timestamp/positive volume、candidate/execution source、方向正确的A股数量规则、T+1/session/limit/liquidity 与 capital commit identity。
- 资本支持的模拟执行 quote 必须嵌入精确`AShareExecutionQuoteEvidence + MarketEvidenceVerification`，绑定dataset/catalog、source receipt/hash/lineage、quote payload、`data_through/observed_at/available_at`、`market_session`、同一calendar receipt和capital/run context。静态时点必须满足`data_through <= observed_at <= available_at <= execution_time`；quote可以在decision后到达，但不得来自future。trade date必须是calendar当前会话，session label必须匹配上海本地effect time。当前模拟只接受 `continuous_auction_am=09:30–11:30` 与 `continuous_auction_pm=13:00–14:57`；收盘集合竞价和盘后固定价不得伪装为连续竞价。整批snapshot先完成静态preflight，随后无默认`TrustedExecutionClock`分别在`sim_submit`与`capital_commit`紧邻副作用前取时，要求`effect_time-data_through <= 30 seconds`并再次复核session；commit校验后、账务写入前还要重读一次最新drift/Champion authority。quote的`execution_time`保持为市场证据时点；模拟`filled_at/terminal_at`取submit副作用时点，全部ISO时间保留原始微秒精度，commit时点不得倒退。第二次检查失败时保留异常reading、丢弃未提交模拟结果、释放预约且不写capital outbox/ledger；倒退或跨交易日时terminal保持最后合法submit。receipt绑定clock identity、`market_session/available_at/data_through`、market execution time、两次effect time与market authority/verification SHA。日循环与对账复用同一session划分和30秒TTL；验证`not_committed`回执时还必须重证`data_through <= available_at <= execution_time <= sim_submit_checked_at`、`sim_submit_checked_at-data_through <= 30 seconds`及`session(execution_time) == market_session`，不能只看commit阶段的最终失败原因。随后按`regression -> trade-date mismatch -> session mismatch -> stale`的唯一优先级证明失败原因；commit阶段的future原因在submit已通过且时钟不倒退时不可达，因此不接受伪造。regression/trade-date的terminal必须精确等于最后合法submit，session/stale必须精确等于commit reading；卖出型零成交回执不得携带release ID。对账另行验证`quote <= submit <= fill/terminal <= commit <= reconcile`，并只为精确允许列表内的commit前市场失效、零成交、完整残量、无fill/commit ID且释放语义一致的`not_committed`回执开放闭环。当前唯一具体时钟是不可继承、`production_eligible=false`的冻结fixture时钟，不是生产time authority；本地最终authority重读也不证明未来外部authority与capital commit已原子化。
- pending/unknown、请求值、弱价格证据或 commit pending 只能作为账户/chain-validation 事实，不能进入策略 PnL。

### A股5分钟 fixture research contract

该合同只用于 `Ashare/minute_data.py`、`Ashare/minute_research.py`、
`Ashare/minute_paper.py` 与 `Ashare/minute_loop.py` 的网络关闭 fixture/mock
验证。它不写 `MarketCapitalLedger`、durable outbox、正式 SampleJournal 或
生产 worker。

- `MinuteDatasetProfile` 只能从一份精确 catalog row 冻结 dataset/schema/fields/
  filters/order/page limit；股票、时间、OHLCV、成交额、前收、停牌、频率、单位换算
  和 raw-unadjusted 价格语义由 TA 显式解释。禁止 SQLite、8082、旧/provider
  route 或文件 fallback。
- `MinuteBarEvidence` 将 provider-native row 与 envelope 的 receipt/lineage/
  `data_through`/`observed_at` 分离绑定；只接受 ready/non-degraded/fresh/valid、
  延迟不超过30秒、完成且合法的主板5分钟bar。相同 `(symbol, bar_end)` 重复/冲突、
  future、午休/收盘跨越、停牌、零成交、分页/游标/same-observation异常均
  fail closed。
- `MinuteFeatureVector` 只含 `close_to_close_return`、`intrabar_return`、
  `range_ratio`、`volume_change`、`amount_change`、显式有效期内的
  `context_adjustment` 及 `raw_rank_score`。当前score固定
  `score_semantics=uncalibrated_deterministic_rank_score`；
  `calibrated_probability=null`、`expected_return_bps=null`、
  `probability_model_state=not_calibrated`、`promotion_eligible=false`、
  `execution_authority=false`。
- `MinuteUniverseInstrument` 的交易成员仅限沪深主板普通股、上市至少30日、
  非风险警示/退市风险，并只路由首批三个研究主题。双创/科创/行业/宽基聚合必须
  `context_only=true`，不能成为 feature symbol、candidate 或 order。
- `MinutePendingFixtureOrder` 只可在完成 bar `t` 后生成，并仅由精确 `t+1`
  完成bar结算；缺失或跳过该bar形成 nonfill，不允许迟到补成交。数据失败取消未结
  新风险并记录拒绝；事件/资金辅助证据必须显式到期，缺失不回退、过期失败关闭。
- baseline/event/flow/dynamic-position 四个 `MinuteFixturePaperBook` 分别从
  50,000 CNY fixture opening state开始，只用于消融；它们不是四个真实账户或
  durable capital authority。每个账本独立执行100股、T+1、费用、滑点、涨跌停、
  bar容量、no-trade band、单票上限、持仓容量、幂等恢复和全mark对账。

### A股 ExecutionRealityModel

当前版本是 `ashare-execution-reality-20260706-v1`，`effective_from=2026-07-06`。adapter、server-local 模拟撮合、共享 market-rules facade 和反事实成本必须读取同一模型，不得各自复制税费/涨跌幅/整手常量。模型依据沪深现行交易规则、中国结算交易过户费及现行印花税口径；规则变动必须发布新 model version，历史订单不得原地改口径。

- `price_limit_policy_version=ashare-price-limit-20260706-v1`：主板正常股及主板风险警示股涨跌幅均为 10%；科创板/创业板为 20%，北交所为 30%；上下限按 0.01 CNY tick 四舍五入。不得继续使用无板块语义的 `st=5%`。
- 买入为 100 股或整数倍；卖出允许100股整数倍、全部退出，或一次性卖出当前完整不足100股余额。不得把非法请求自动向下取整、拆成部分零股或改写为其它数量，且T+1可卖量必须覆盖请求。`lot_rules.version=ashare-lot-rules-20260706-v1`。
- `session_policy_version=ashare-sessions-20260706-v1`：连续竞价只包括 `09:30–11:30` 与 `13:00–14:57`。`14:57–15:00` 是独立收盘集合竞价；`15:05–15:30` 是面向全部 A股的独立盘后固定价格交易，order type 为 `after_hours_fixed_price`，价格引用正式收盘价。当前同步模拟器没有集合竞价批量撮合或盘后固定价撮合，因此两者都必须返回显式 unsupported reason；不得延长普通连续竞价伪造成交。observation/counterfactual 仍继续记录。
- 连续竞价限价申报使用 `ashare-continuous-price-cage-20260706-v1`：买价上限为“基准价 102%”与“基准价 + 10 ticks”的较高者；卖价下限为“基准价 98%”与“基准价 - 10 ticks”的较低者。tick 为 0.01 CNY，执行样本保存可验证基准价来源。
- 撤单使用 `ashare-cancel-cas-20260706-v1`，保存 `state_version`、expected/observed state version 和 cancel outcome；成交/终态先到时不得回写成已撤。未来异步 broker 必须使用 append-only order events + startup reconcile，当前 sim-only 同步引擎不构成 broker-ready 证明。
- 卖方证券交易印花税为 5bps；交易过户费买卖双方各 0.1bps，分别保存 `stamp_duty` 与 `transfer_fee`，不得合并进 commission。当前保守佣金暂按 2.5bps、最低 5 CNY，`commission_schedule_status=provisional_pending_broker_contract`；它不是已核实的华创费率。只有实际合同/交割单核实后，才允许以 `broker_contract_verified`/`broker_statement_verified` 和独立 `commission_schedule_version` 覆盖。
- 成交费用记录至少包含 `commission`、`stamp_duty`、`transfer_fee`、总费用（模型为 `total`，本地成交/回执为 `total_fee`/`fee`）、`execution_reality_model_version`、`commission_schedule_status` 和 `commission_schedule_version`；实际成交绩效最终使用回执/交割事实，保守模型只用于模拟与反事实。

## 本地自动模拟日 RunBundle（候选）

contract ID：`tradingagent.paper_day_loop.v1`。该对象只是编排证据，不取代 capital、position、order、fill、reconcile 或 SampleJournal authority。

```yaml
RunContext:
  trade_date: YYYY-MM-DD
  decision_as_of: timezone-aware Asia/Shanghai instant on trade_date
  market: ashare
  authority_id: ashare-capital-v1
  authority_generation: positive-current-generation
  execution_lineage: immutable-lineage
  account_type: simulated
  real_trading_enabled: false
  champion_manifest_sha256: 64-hex
```

`decision_as_of` 是 run identity 的必需组成部分，与 `trade_date` 在上海时区不同日时 fail before stage。阶段顺序固定为 `preopen -> evidence_ready -> universe_ready -> decision_ready -> risk_checked -> orders_simulated -> reconciled -> learning_recorded -> reported`。每个 StageReceipt 绑定 component ID/version/artifact SHA、input bundle SHA、输出 payload SHA、reason codes 和幂等 key。

evidence/universe/preopen 在早期阻断新增风险时，loop 不得把无候选交给 optimizer 后异常退出。它要生成可审计 `hold`，继续处理已验证持仓的 reduce/exit、reconcile、Decision Ledger、label maturity 与 report，并以 `completed_with_blocks` 结束。position authority 无效时只能严格 hold，不能借“继续闭环”生成任何方向订单。

`compose_paper_runtime`和fixture CLI只允许精确`FrozenFixtureStagePort`及不可晋级fixture账户/proof，禁止任意callable或网络/broker port以自报属性通过。`compose_capital_backed_paper_runtime`是另一条test-only composition：它只通过public stage contracts连接canonical simulated ledger、人工选择Champion、逐副作用drift/Champion复核、capital outbox、模拟成交与reconcile；当前没有CLI、scheduler或live sample。FileRunBundleStore只使用调用方显式给定的本地root，每个compare-and-swap先写同目录临时文件、完整写入并fsync、原子公布为递增的内容寻址事件，再fsync目录。reader校验完整事件链；中断的temp不是有效事件。无默认生产目录，不读旧runtime fallback。

## Decision Exposure Ledger（audit-only）

`decision_exposure_disposition` 事件只能是：

| disposition | 必需事实 |
|---|---|
| `PAPER_FILLED` | 通过风险的模拟订单 + 内容绑定 fill receipt |
| `PAPER_NOT_FILLED` | 通过风险但明确未成交 + nonfill reason |
| `REJECTED` | 明确结构化 risk rejection |
| `OBSERVATION_ONLY` | hold/不交易，无任何成交或拒绝伪装 |

每条记录绑定 `source_run_id`、`input_bundle_sha256`、capital authority/generation、execution lineage、decision/prediction/cluster identity、requested notional、模拟 fill 和 actual cost。读回必须按当前 run 的整组 identity 限定，禁止仅用可重复的 `order_id` 扫描其他 run。这些事件始终为 `eligible_for_statistical_learning=false`、`eligible_for_performance_metrics=false`、`eligible_for_calibration=false`、`eligible_for_promotion=false`；它们只用于完整决策覆盖和反事实实验设计。

## ValidationPlan 与交易会话 authority（候选）

A股 `ValidationPlan` 必须绑定 `TradingSessionCalendarAuthority`：market、calendar ID/version、source dataset/receipt ID/SHA、带时区 `available_at` 和严格递增的完整 session tuple；并强制注入无默认`TradingSessionCalendarAuthorityVerifier`，将verifier ID/version、verified time、proof hash和计划`frozen_at`一起绑定。calendar必须在计划冻结前可知，canonical plan hash同时绑定calendar/proof/session count。train/validation/test六个边界必须都是该calendar会话，purge/embargo按边界之间的交易会话数计算；非A股才保留自然日间隔。

本地forward-label合同要求`ValidationPlan.frozen_at <= prediction_at`，从同一verified calendar派生`close/1d/3d/5d`的上海15:00时点；调用方target只能作相等断言，label/result逐项保存plan/calendar/proof SHA，缺目标会话日线不得顺延。`SampleJournal.materialize_labels()`、`materialize_label_batch()`、`run_ashare_forward_label_ops()`和`run_ashare_sample_ops()`均要求显式传入该计划；A股调用缺失时在读取行情前fail closed，非A股仍可使用原自然日合同。

两个A股 CLI 只接受`--validation-plan-path`指向预先生成的内容寻址artifact。加载器要求`artifact_type=ashare_validation_plan_v1`、canonical `validation_plan` payload和匹配的`validation_plan_sha256`，重建calendar/detached proof并拒绝symlink、缺字段、非canonical payload或任一hash/binding漂移。它不会调用`TradingSessionCalendarAuthorityVerifier`、不会在运行时生成或重签proof，也不会根据观测到的bar反推日线target。当前loader只验证artifact内部合同与内容绑定，不验证顶层authority tier或生产registry；因此它只能用于本地sim-only候选，不能把fixture source receipt升级为真实calendar authority。

这些门能防止周末、节假日、调用方自授target authority和运行时自签，却不证明fixture source receipt、exit price或收益真值真实。当前没有生产calendar verifier、受信计划artifact registry或真实market-truth readback；任何predictive eligibility仍须经过frozen OOS、总回报、公司行动与独立authority门禁。

## Label maturity authority（候选）

`market_truth / paper / shadow / unavailable_oracle` 是不同证据类，不得互相伪装。只有`source_class=market_truth`才可能进入predictive validation，并且必须同时满足：

- horizon 已到期、`available_at <= assessed_as_of`；
- canonical evidence payload 经本地重算 SHA-256，decision cutoff、horizon、value 和 receipt IDs 精确一致；
- 外部注入的 `FrozenAuthorityProof` 绑定同一 evidence SHA/receipts 和冻结时点；
- 外部注入、默认不存在的 verifier 以完整 binding 验证 proof；
- 决策前已冻结且内容hash自校验的`FrozenOOSValidationPlanReceipt`绑定registry、plan、主horizon和eligible source class；
- total-return definition、corporate-action policy与OOS receipt中的版本完全一致；
- adjustment-truth receipt/hash位于source receipts中，覆盖完整horizon，且其可知时间不晚于label available time；
- 独立OOS registry verifier在每次投影重建时重新验证receipt，手工构造verification不能自证。

proof、OOS receipt、总回报/公司行动真值或任一verifier缺失，自报verification、binding/时间不一致均不得发布predictive eligibility。fixture永远不能成为release evidence；paper只供模拟执行验证，shadow只供反事实验证，unavailable只保留不可用原因。

## 模型漂移与负向进化（本地候选）

漂移证据必须绑定`journal_head_sha256`、`model_manifest_sha256`、`metrics_artifact_sha256`、metrics实现版本、带时区window/evaluated time和`effective_independent_sample_count`。数值生产者不得在 metrics artifact 内自报 `lineage_verified`；当前 `tradingagent.drift_metrics_artifact.v2` 只有在另一份 `tradingagent.drift_metrics_verification_receipt.v1` canonical detached receipt 同时绑定 exact artifact/evidence SHA、固定本地metrics implementation trust root、label/cost snapshot SHA、journal/model、window、horizon、regime、sample count 与非空 source receipt 集合后，控制器才构造 verified evidence。verifier重新读取canonical artifact/receipt并复核所有字段，拒绝调用方选择任意implementation。receipt 缺失、非 canonical、实现/窗口/证据绑定漂移或 producer 重新加入自证字段均 fail closed。proof SHA只是本地完整性hash，不是数字签名；该本地 verifier 合同也不等于真实独立 metrics 重算 authority。样本不足、证据过期、lineage/数据异常、校准/OOD/成本偏差不得由调用方用一个`healthy=true`覆盖。

控制器必须显式注入`TrustedEvolutionClock`，没有进程墙钟或调用方timestamp回退。clock reading绑定model manifest和evidence SHA，结果保存`trusted_clock_identity_sha256`与`trusted_evaluated_at`；reading不得早于metrics记录/评估时点，metrics超过14天视为过期。当前唯一具体实现是不可继承、不可变、`production_eligible=false`的`NonProductionFixtureEvolutionClock`，只证明测试中的确定性freshness门，不是长驻生产时间authority。

自动动作仅允许`quarantine / reduce_only / stop_new_risk / require_review`。动作以内容寻址receipt持久化，active latch只允许保持或收紧；进程重启后即使新窗口看似健康，也不能自动清除、晋级模型或扩大风险。恢复必须经过独立人工复核和未来版本化的显式release流程。

“保持或收紧”同时约束两个维度：新 receipt 的 `risk_multiplier` 不得高于 active receipt，动作集合也不得丢失现有的 risk-action/review 严重度，且至少一个维度真正收紧。未知 action severity 直接 fail closed。当前wrapper在每次risk评估前重读store，网络关闭的simulation wrapper在模拟副作用前再次重读；如果新latch收紧，open/increase强制转为未成交，reduce/exit不被抹掉。该实现仍不是live scheduler或broker authority；未来真实外部副作用前必须执行并记录同一类最新latch复核。

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

A股`close/1d/3d/5d`的target必须从预测前冻结且由独立verifier复核的同一`ValidationPlan`交易会话authority派生；调用方提供的target只能断言完全相等。label/result逐项保存plan SHA、calendar SHA与detached proof SHA。目标会话缺15:00日线时保持missing，不得顺延到后一会话或使用次日开盘补造。该target authority只证明目标时刻，不证明exit price、total return或公司行动真值；predictive eligibility仍需下游market-truth/OOS/adjustment authority。

- `as_of` 限制可见数据；日线不能伪造 m30/m60，晚到价格不能回填更早 horizon。
- 科学 PIT 证据必须同时保存并重新校验 `event_time <= available_at <= ingested_at <= retrieved_as_of <= prediction/label as_of`；source SHA 或任意 `as_of` 字段不能单独证明 PIT。reference/entry 与每个 exit candidate 都必须在排序、选价和计算收益前通过同一个 Evidence Gate，且 validation 必须 `complete=true,status=valid`。EvidenceEnvelope 在 record root、PIT root、PIT `timestamps` 与 adapter 原始 envelope 收集所有 present event aliases（包括 `event_time/source_event_time/timestamp/observed_at/bar_time/trade_time/datetime`）；它们必须换算到同一 UTC instant，同义 `+08:00`/UTC 允许，任一非法、naive 或冲突 fail closed。receipt/availability aliases 至少覆盖 Journal 的 21 条 root/nested 路径，并额外覆盖 provider `published_at/retrieved_at/collected_at_dt`；每个 present 值必须带时区且可解析，最晚证据时刻不得晚于本轮边界，较早字段不能覆盖较晚字段。validated envelope 的 canonical 四钟必须与 nested lineage 一致；窗口资格、排序、`evidence_at` 与写出 lineage 只使用该 canonical instant。任一顺序冲突、future receipt 或 canonical instant 超窗的 point 不能影响候选排序，也绝不能生成 `ready/verified_exit_evidence`。
- 原始 reference/entry collector 在选择前排除 PIT 失败 row；如果没有任何合法 row，候选保持 retryable `pending_reference_evidence`/degraded，不携带无效价格或 PIT。若一个已选中/已持久化的 reference/entry 声称有价格但其 lineage present-invalid，则为 `rejected_data_quality`。exit PIT 失败在可能由后续合法行情恢复时保持 retryable `missing_exit_evidence`/degraded。缺或非法 PIT 不删除 observation，也不伪造 terminal price/label；只有后来到达且独立通过 Evidence Gate 的合法 point 才能恢复该 horizon。
- CNFutures prediction writer 必须把 TradingDatas 实际 HTTP response receipt 连同 source event aliases、原始 bar 和 nested PIT 持久化到 immutable source snapshot；session review 与 forward-label adapter 必须原样传递该 envelope。合法 receipt 参与 prediction/data-as-of 边界，reference 与 exit 都可 ready；missing/invalid/naive/future/conflicting receipt 一律 non-ready。HTTP receipt 是 transport 实际接收事实，不得由任务 `as_of`、prediction/bar time 或当前墙钟代填。历史缺 receipt 的记录保持 pending/degraded。
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

- `marketSummaries[]` 按 market + currency + account authority 保存 capital authority ID、generation、maturity 和市场自己的资本/PnL/return/DD；缺字段显示 null/unavailable。
- All Markets 不生成 combined monetary portfolio/performance；只可汇总非货币 counts/health。
- `portfolio.ashareAccount` 只显示 A股账户事实；CNFutures 与 Crypto 使用各自 market summary 和原生币种。即使 A股与 CNFutures 同为 CNY，也属于不同 authority；不同 market/account 的 capital、equity、PnL、return、drawdown、utilization 禁止聚合。
- 前端只读；不得创建/修改 signal、capital、sample、email、callback 或 execution state。

## 版本与变更

- schema、style、selection policy、decision policy、cost model、authority/generation 和 execution lineage 随记录保存；历史事件不原地补字段。
- 字段变更必须同步代码、测试、本契约和 [operations.md](operations.md)。
- 旧共享资本与旧演化入口没有兼容写路径；只读历史不得进入当前统计。
