# A股 Phase 1 真实观测与自动模拟闭环实施计划

> **Historical implementation record (2026-08-22):** 本计划中的已完成项、旧任务
> 顺序和固定批量只保留为历史语境，不是 current 能力、活动 backlog 或模拟准入门禁。
> 当前入口为 `../../BACKLOG.md`、`../../STATUS.md` 和本轮运行读回；安全子集不等待
> exact500、全市场或全部下游证据。候选、main、GitHub、服务器文件、运行时、真实
> 数据和真实交易权限仍分别验收。

**目标：** 在 MarketGraph 物理 `mg_off`、`REAL_TRADING_ENABLED=false` 的前提下，先让 TradingAgent 通过 TradingDatas 正式 `GET /v1/catalog` 与 `POST /v1/query` 自动积累 A 股主板 current-observation 数据，再把通过可信时钟、交易日历、市场证据、Champion、风险和资本门禁的数据接入 50,000 CNY canonical paper-sim 日闭环。

**架构：** 两条不可混淆的流水线。Observation worker 只读取 TradingDatas，先写 durable intent，再构建 immutable research snapshot、probe receipt、aggregate observation receipt 和逐股 membership ledger；四项精确读回后发布 transaction-complete commit marker，永不导入资本/订单模块。Paper worker 只消费这五项 committed binding 与 forward-collected history。在 next-session calendar、数值 feature、分钟/L1 及独立 production-style simulation authorities 全部通过前，它固定产生 `paper_trade_session=null` 的 `abstain/completed_with_blocks` 证据；只有停止线解除后才能进入候选、模拟预约/成交、durable outbox、资本 commit、T+1/费用校验、日终 reconcile 和 SampleJournal。前端不持有 TradingDatas token，只消费 TA 自己的只读投影。MarketGraph 不在两条关键路径中，`mg_off` 必须独立成立。

**技术栈：** Python 3.11、pytest、现有 `SharedSignalsV1Client` 兼容符号、TradingDatas Bearer token-file transport、`ResearchDataSnapshot`/`FileResearchSnapshotStore`、现有 capital/outbox/reconcile primitives、systemd one-shot service/timer（专用 `tradingagent` 服务身份，默认 disabled 直至运行验收）。

**固定边界：** 只分析/模拟沪深主板个股；创业板、科创板、北交所个股零泄漏，其指数和行业汇总仅作环境上下文；V1 Champion 仅是 `uncalibrated_deterministic_rank_score`；无 DeepSeek/MarketGraph/经纪商/真实账户/邮件/公开入口；无 SQLite、`/tushare`、`/source_status`、旧 8082 或文件 fallback。

---

## Task 1（已完成）：将正式 TradingDatas HTTP transport 从测试命名空间提取到数据层

**Files:**

- Create: `shared/data/tradingdatas_transport.py`
- Modify: `shared/runtime_test/sharedsignals_v1_gate.py`
- Test: `tests/test_tradingdatas_transport.py`
- Test: `tests/test_tradingdatas_bearer_auth.py`

**TDD：**

1. 新测试先证明正式模块必须具备 canonical authority、loopback HTTP、精确两路由、Bearer header 私有注入、401/403 latch、并发单飞、redirect/proxy 禁止、4 MiB 上限和无 fallback。
2. 运行聚焦测试确认 RED。
3. 移动最小实现；旧 `runtime_test` 模块只做兼容导出，避免双实现。
4. 运行聚焦测试及现有认证回归确认 GREEN。

**Commit:** `feat(data): promote TradingDatas runtime transport`

## Task 2（已完成）：实现 observation-only A股正式数据 runner

**Files:**

- Create: `shared/runtime/ashare_observation.py`
- Create: `tools/run_ashare_observation.py`
- Create: `tests/test_ashare_observation_runtime.py`
- Modify: `shared/runtime_test/sharedsignals_v1_integration_probe.py`（只允许提取共用 manifest/profile loader；不得放宽 probe）
- Modify: `docs/data_contract.md`
- Modify: `docs/operations.md`

**合同：**

- 必须使用仓外绝对 manifest 和 token-file；manifest 固定 `base_url/catalog_version/schema_major/dataset IDs/fields/filters/as_of/order/limit/budgets/identity fields`，不包含 secret。
- 先完成 catalog、bounded cursor、跨页 identity、same-observation 双跑和 envelope metadata gate；任一失败在任何持久写前停止。
- 构建并 CAS 落盘完整 `ResearchDataSnapshot`，额外写 immutable aggregate observation receipt；Task 2 冻结的旧 runner 只形成 `snapshot + probe receipt + observation receipt` 三件绑定。逐股 membership ledger、transaction intent/complete 与五项 committed binding 属于后续 Task 5，只能在 fresh state 中生成，不得倒填或升级旧状态。`authority=non_authority`、`production_verified=false`、`historical_pit_eligible=false`、`real_trading_enabled=false`、`marketgraph_mode=mg_off`。
- 只允许主板个股进入 `observation_universe`；该集合只是通过板块、上市状态、风险警示、上市天数和当日日线完整性的观察初筛，不等于通过小资金价格/流动性/成本/组合门禁的可下单股票池。兼容读回可暂时保留旧 `tradable_*` 别名，但新账本与文档以 `observation_universe_*` 为权威命名。创业板/科创板/北交所个股不得进入候选或行级输出，相关指数/行业数据只进入 `market_context`。
- `index_classify`/`sw_daily` 只是行业分类与行业指数环境，没有成分 denominator 与 coverage authority 时不得称为完整行业宽度。
- exact replay 幂等；重复执行不得重复联网后追加第二份 authority，也不得产生 capital/outbox/fill/reconcile 文件。

**TDD：**

1. 写入 401/403、degraded/stale/failed、分页循环/预算、identity/order drift、主板泄漏、伪 PIT、MG 特征、资本副作用、重放冲突等失败测试。
2. 运行聚焦测试确认 RED。
3. 用 `SharedSignalsResearchEvidencePort`、`FileResearchSnapshotStore` 和正式 transport 做最小组合。
4. 运行聚焦 + probe/pagination/snapshot 回归确认 GREEN。

**Commit:** `feat(ashare): add TradingDatas observation runner`

## Task 3（候选已完成，未激活）：增加专用 A股 worker 安装候选，前端保持不变

**Files:**

- Create: `deploy/systemd/tradingagent-ashare-observation.service`
- Create: `deploy/systemd/tradingagent-ashare-observation.timer`
- Create: `deploy/systemd/tradingagent-ashare-worker.env.example`
- Create: `deploy/systemd/tradingagent-runtime.tmpfiles.conf`
- Create: `tools/audit_ashare_worker_runtime.py`
- Create: `tests/test_ashare_worker_units.py`
- Create: `tests/test_ashare_worker_runtime_audit.py`
- Modify: `docs/operations.md`
- Modify: `docs/system_state_matrix.md`

**合同：**

- dedicated `tradingagent:tradingagent`；release root 只读；state/runtime/log roots 分离；token leaf 固定 `/run/secrets/tradingagent/tradingdatas-read.token`、`0600`、owner 为服务 UID、regular/1-link/path-no-follow。
- unit 固定 `REAL_TRADING_ENABLED=false`、loopback-only、`mg_off`，不包含 broker/LLM/public API；front unit 不改、不读 token。
- timer 作为代码候选默认 disabled；未完成一次真实 one-shot 与重启/幂等验收前禁止 enable。
- 不复用 `marketgraph` 共享 UID 的 token 作为持久调度凭证；现有 token 只可用于隔离的一次性只读兼容验收。

**TDD：**

1. 先写 unit sandbox、owner、secret、禁止词、disabled 与 audit 负例测试。
2. 运行确认 RED，再加最小 units/audit。
3. `systemd-analyze verify`（若本机可用）和 pytest 均通过。

**Commit:** `feat(runtime): add dedicated A-share observation worker candidate`

## Task 4（已完成）：正式 TradingDatas 一次性只读验收与首份真实观测

**Server-only preflight / no repo secret:**

1. 读回 formal `127.0.0.1:18082`、服务状态、catalog 版本、token leaf 元数据和当前 TA release；不读取/输出 token。
2. 使用现有只读 token 仅运行 isolated one-shot catalog/query/integration probe；精确日分区查询，禁止无界 daily 查询和重试 fallback。
3. 在隔离 state root 运行一次 observation runner；验证 snapshot/receipt hash、mode/owner、主板零泄漏、无 capital/order/outbox/reconcile 副作用。
4. 重跑同一 decision-as-of 验证幂等；随后移除临时进程，保留证据。
5. 任一数据/认证/身份/游标错误则保持 observation unavailable，不启用 timer。

**Evidence:** 服务器 candidate 路径、release SHA、manifest SHA、receipt SHA、snapshot SHA、测试命令和退出码；消息与日志不得包含 token。

2026-07-22 fresh 结果见 `docs/reports/2026-07-22-ashare-observation-readback.md`。正式 `127.0.0.1:18082` 五数据集 bounded/same-observation probe 通过，首份主板 observation 产生 3041 个观察标的；精确重放返回同一 snapshot/receipt 且 `idempotent_replay=true`。该旧 `a7488e9` one-shot 只写了 snapshot/probe/observation receipt，没有新契约的 membership ledger、forward history 或 paper planner，因此新候选必须使用 fresh state root，不得补写旧状态或继承其服务器验证。这只证明一次性服务器只读观测，不代表专用 worker、timer、历史 PIT、模拟成交或生产 runtime 已激活。

## Task 5（候选实现中，待冻结验收）：补齐真实快照到自动 paper-planning 的 production-style authority ports

**Files:**

- Create: `shared/runtime/ashare_runtime_ports.py`
- Create: `shared/runtime/tradingdatas_market_evidence.py`
- Create: `shared/runtime/ashare_observation_ledger.py`
- Create: `shared/runtime/ashare_observation_history.py`
- Create: `shared/runtime/ashare_paper_planning.py`
- Modify: `shared/runtime/trusted_clock.py`
- Modify: `shared/runtime/composition.py`（如需新增独立 planning composition；原 fixture/capital composition 原样 fail closed）
- Modify: `shared/runtime/capital_stages.py`（仅当真实分钟/L1 authority 进入时，精确允许新的 final authority 类型；禁止 duck typing）
- Create: `tests/test_ashare_runtime_ports.py`
- Create: `tests/test_tradingdatas_market_evidence.py`
- Create: `tests/test_ashare_observation_ledger.py`
- Create: `tests/test_ashare_observation_history.py`
- Create: `tests/test_ashare_paper_planning.py`

**生产式模拟 authority：**

- Task 5 只在 fresh state 上把 observation 升级为 `snapshot + probe receipt + observation receipt + membership ledger + transaction-complete commit proof` 五项 committed binding，并让 history/planner 精确消费该绑定；旧 `a7488e9` 三件服务器状态永久保持历史只读。intent 存在但 complete 缺失时属于可恢复半写事务，绝不具备 runtime/history/planning eligibility。下游资格只能由 `load_verified_ashare_runtime_authority_bundle` 在同一私有 state root/session lock 内重读五项证据后产生；公共 mapping/hash/dataclass builder 永远不能自授 eligible，market mark lineage 继续绑定 membership 与 complete identity。

- `TradingDatasCalendarAuthority`：只接受已冻结 trade calendar snapshot，生成 detached session proof。
- `SealedRuntimeClock`：时间由一次 run manifest 密封，拒绝任意 callable/naive/local clock。
- `TradingDatasMarketEvidenceAuthority`：日线只允许从同一冻结 snapshot 生成“上一已验证交易日”的持仓估值 mark；行身份与 envelope receipt/lineage 绑定。同日 observation 不得冒充上一交易日 mark。
- `MainboardUniverseAuthority`：证券主数据 + daily snapshot 双证据，创业板/科创板/北交所个股零泄漏；指数/行业只作 context。
- `FrozenChampionAuthority`：只实现当前确定性 rank 与固定 probe sizing；`calibrated_probability=null`，LLM/MG/shadow sleeves 不得改变订单。
- `PersistedThesisRiskAuthority` 和 numeric feature/coverage verifier：无默认、版本/哈希/有效期齐全。

**分层资格：** 必须分别输出 `observation_eligible`、`ranking_eligible`、`planning_eligible`、`execution_evidence_eligible` 和 `blockers`。`ResearchDataSnapshot.execution_eligible` 只表示研究数据 gate 通过，不得解释为订单或成交资格。历史特征未齐时 Champion 必须 abstain，不能用默认值补齐。

**历史与标签停止线：** 20 日 momentum/volatility 至少需要 21 个 forward-collected session；但在独立交易日连续性与公司行动/复权 authority 缺失时，计数达到 21 也仍然 blocked。当前 membership ledger 固定 `label_horizons=[]`、`learning_eligible=false`；没有 T+1 calendar 和分钟/market-truth 锚点时不生成或回填标签。

**分钟证据停止线：** 当前 TradingDatas 五个 active dataset 中，行情粒度只有日线，没有分钟/L1；不能从 `open/high/low/close/vol/amount` 合成 bid/ask、盘口数量、30 秒 freshness、分钟成交量或 T+1 可卖量。缺少冻结且可验证的分钟/L1 数据时，稳定输出 `minute_execution_evidence_unavailable`，不得 reserve、fill、outbox 或 capital commit。若未来只有分钟 OHLCV 而无 L1，则另建 bar-evidence/fill 合同；绝不把 bar close 复制成 bid/ask。

**TDD：** 每个 authority 先覆盖缺字段、跨 snapshot、过期、重复 symbol、非主板、错误交易日/时区、伪概率和 context 越权；planning composition 必须不接受 transport/network/LLM/broker，并证明 daily-only 条件下以 `completed_with_blocks` 或等价终态关闭当天运行且资本账本无副作用。

**Commit:** `feat(ashare): compose production-style paper simulation authorities`

## Task 6：实现 one-shot 自动 paper day 与重启恢复

**Files:**

- Create: `tools/run_ashare_paper_day.py`
- Create: `shared/wrappers/job_ashare_paper_day.sh`
- Create: `tests/test_ashare_paper_restart_recovery.py`
- Create: `tests/test_ashare_scheduler_contract.py`
- Modify: `deploy/systemd/tradingagent-ashare-observation.service`（改名/拆分为 worker service，如需）
- Create: `deploy/systemd/tradingagent-ashare-paper-day.service`
- Create: `deploy/systemd/tradingagent-ashare-paper-day.timer`
- Modify: `docs/operations.md`
- Modify: `docs/system_state_matrix.md`
- Modify: `STATUS.md`

**日闭环：**

当前两阶段链路必须显式区分，并固定交易时序：日线 observation 的 `observation_session=T`，数据真正可知时点为 `available_at`，`decision_at` 必须晚于 T 日收盘且不早于 `available_at`；任何潜在 paper trade 只能指向由冻结交易日历证明的 `paper_trade_session=T+1`。当前日线不得产生 T 日订单，也不得把 post-close 数据称为 pre-open 证据。

```text
日线/历史不足：
post-close observation manifest(T) → observation snapshot → mainboard observation universe
→ history/feature readiness → abstain 或 counterfactual plan
→ blocked reason / Decision Ledger / read-only projection

分钟/L1 authority 齐全后：
counterfactual plan → 50k optimizer → hard risk/reservation
→ simulated fill/outbox → capital commit → close MTM reconcile
→ SampleJournal / Decision Ledger / read-only projection
```

**TDD：** 覆盖 partial fill、T+1、100股、费用/滑点、15%单票/90%总敞口、8仓、日亏/连亏/回撤、crash before/after outbox、幂等重放、stale snapshot、restart、同股同日唯一订单和 MG/LLM 无 authority。

**发布停止线：** observation timer 与 paper timer 分开。当前 observation service/timer 只是 non-enableable code candidate：专用 TA token/service identity 与可信每日 immutable manifest rollover 未完成前禁止 enable，也禁止用静态 manifest 日复一日回放相同 `as_of`。分钟/L1 authority 不齐时 paper timer 即使运行也只能记录 `paper_trade_session=null` 的 `abstain/completed_with_blocks` plan，不得生成模拟成交。one-shot 两次（含 crash-replay）一致且 P0/P1=0 后才允许启用对应 timer；第 5、10 个交易日人工复核；20 个连续单日收益区间与至少 21 个观察 session 前不把结果称为有效策略或已校准模型，且这些重叠市场路径样本不假设统计独立。

**Commit:** `feat(ashare): add automatic canonical paper day loop`

## Task 7：独立评审、合并、发布和运行读回

1. 每个任务由新鲜 reviewer 检查范围、数据合同、安全、科学性和测试；P0/P1 清零，P2 进入 backlog。
2. 运行所有聚焦测试、`REAL_TRADING_ENABLED=false python3 -m pytest -q`、`git diff --check`、secret/legacy/MG/broker grep、文档与机器状态一致性检查。
3. 精确提交并推送候选，创建 PR，等待 CI，通过普通 merge 合入 `main`；再次读回 `main==origin/main`。
4. 先部署 immutable candidate 并运行 observation one-shot；专用 UID/token/目录属于生产权限变更，只有在当期明确纳入且 preflight/rollback 可验证时执行。
5. observation timer 和 paper timer 分开启用；每次启用后读回 unit、用户、env、token 元数据、端口、状态目录、最近 receipt/snapshot/journal、无 broker/no public ingress/no MG。
6. 回滚只停止 timer/worker、切回旧 immutable release并保留 append-only state；不得删除 snapshot、ledger、outbox、journal 或恢复旧 8082/SQLite/legacy reader。

## 完成定义

- **工程观测闭环完成：** formal TradingDatas 真实 one-shot + timer 持续产出 immutable observation，5 个交易日重启/幂等/数据漂移验证完成。
- **工程模拟闭环完成：** canonical 50k simulated capital/outbox/reconcile/SampleJournal 连续自动运行，crash-replay 验证通过，至少 21 个 forward-collected observation session（对应 20 个连续单日收益区间，不假设统计独立）完成，且 calendar/adjustment/market-truth authority 均有独立 readback。
- **尚不代表：** 策略有效、概率已校准、收益为正、可实盘、可自动晋级或已启用真实交易。
