# System State Matrix

> 本文解释系统状态治理；当前能力状态的机器可读事实源是 `shared/governance/system_state_matrix.yaml`，旧链消费者与退役门的机器可读事实源是 `shared/governance/legacy_inventory.yaml`。本地候选、Git 主线、服务器旁路、生产文件、生产 runtime、已安装 cron、真实数据与真实交易动作必须分别验证。A股 V1 与第一批三市场内核已经进入 Git 主线；本轮非核心市场物理退役在 `codex/ta-postrelease-state-retirement-v1` 独立候选中收口，并在合并后成为 `repository_contract`。该层与 `production_verified=false` 只证明仓库合同，不能把代码存在或测试通过静默提升为 runtime、真实数据或交易能力。

`legacy_inventory.yaml` 中的 `paths` 只登记干净克隆必须存在的源码或配置；`runtime_paths` 单独登记可能尚未生成、不得纳入 Git 的安装态/运行态历史路径，并要求由 `.gitignore` 明确覆盖。运行目录在某台开发机上存在或不存在都不能证明生产消费者已经退役，生产裁决仍需独立的 installed-runtime readback。

## 状态语义

| 状态 | 含义 | 可以做什么 | 不能推断什么 |
|---|---|---|---|
| `CURRENT_VERIFIED` | 本轮在所标注 layer 已有新鲜证据 | 仅在该 layer 和 allowed uses 内消费 | 不能自动推断生产或真实业务动作 |
| `TARGET_CONTRACT` | 目标契约正在隔离实现或验证 | fixture、契约测试、候选开发 | 不能宣称 runtime 已切换 |
| `PLANNED_NOT_IMPLEMENTED` | 只有预注册路径和门禁 | TDD 与接口设计 | 不能注册为当前能力 |
| `COMPATIBILITY_TIMEBOXED` | 有明确消费者和退役门的临时兼容 | 只读、限定消费者 | 不能成为新依赖 |
| `HISTORICAL_READ_ONLY` | 仅有历史证据，本轮未验证 | 审计线索 | 不能表示当前仍运行 |
| `RETIREMENT_PENDING_VERIFICATION` | 已有替代目标但删除条件未通过 | 引用扫描和 parity | 不能直接删除或继续扩展 |
| `RETIRED_BLOCKED` | 已完成清零、禁止恢复 | 历史审计 | 不能重新接回生产链 |

## 当前关键边界

- TradingDatas `GET /v1/catalog` 和 `POST /v1/query` 由上游唯一 writer/reviewer 负责；TA 只实现可配置 consumer，不读取或修改 TradingDatas 仓，也不把 HTTP 200 或 catalog metadata 当成 authenticated parity。review #23 / `1907df9` 已把 catalog-driven parity 与专用 identity/front 合同合入主线，但机器状态继续是 `TARGET_CONTRACT / repository_contract / production=false`。旧九 profile fixture 只证明 190/9/181、5 ready/4 impaired 的本地合同；最新上游 handoff metadata 是 production `c3232d0…`、`catalog_version=v1-fcc1aaa39c20743e`、190/12/178、5 ready/7 impaired，新增三项为 `partial/degraded`，`cn.dataset.suspend_d` 为 `stale/degraded`。TA 12-profile authenticated parity 仍为 `NOT RUN / BLOCKED`，等待覆盖全部 12 项的获批 secret-free profiles 与 fresh TA credential；本文不声明二者存在。2026-07-22 使用旧 `marketgraph` credential 的五项 one-shot 仍只属历史兼容证据，不能扩张为 latest active-set parity。
- local/server identity preflight 已创建 `tradingagent:tradingagent` UID/GID 987 和 immutable release，但没有切 current/front、fresh token 或 tmpfiles，timer 仍未安装/未启用。既有 `/run/secrets/tradingagent` 只做过 metadata-only 读回：parent 为 `0700 marketgraph:marketgraph`，叶为 `0600 marketgraph:marketgraph` regular、`nlink=1`；这不是 TA credential handoff。后续固定先 pause TA cron并读回零 TA job/保留 non-TA，再证明零 process/cgroup/cwd/root/open-FD/mmap holder，随后协调 credential freeze、应用 tmpfiles、由 publisher 原子安装 fresh TA 叶、metadata-only readback、最后 unfreeze。pause 后失败必须保持 unavailable；禁止恢复旧 credential/TA cron或回退 8082。
- A股资本、执行 lineage 与 SampleJournal 是仓库契约层当前能力；它们不证明生产 runtime、cron 或真实市场样本已验证。
- 主板 scope、Phase 1.5 行业 shadow 薄切片、OpportunityRadar/Ledger、多期限 forecast、三风格 router、LLM evidence/journals、V1 client、小资金与资本/风险/执行合同仍主要是仓库级能力；服务器现役必须逐项另验。新 A股候选将 observation 权威收紧为 snapshot/probe receipt/observation receipt/membership ledger 四项数据证据加 transaction-complete commit proof 的五项绑定；只有同一私有 state root/session lock 内重读五项证据的 strict loader 才能生成 verified bundle，普通 mapping/hash/dataclass 不能自授资格。`observation_universe` 不是 tradable/order universe。每个 symbol 至少 21 个 forward session 只是 20 日特征的最小数学覆盖；缺交易日连续性和公司行动/复权 authority 仍固定 blocked。ledger 当前无 label horizons；daily-only planner 固定 `paper_trade_session=null` 且 abstain，无资金/订单/成交副作用。`index_classify`/`sw_daily` 只是行业分类/行业指数环境，不证明完整行业宽度。
- 当前 DeepSeek 候选接受两种精确transport：同时绑定request/outbound identity的冻结离线响应fixture，以及默认关闭、固定官方HTTPS地址、禁代理/重定向/自动重试的`DeepSeekHTTPTransport`。2026-07-18一次旧A股v1 Prompt的隔离真实请求到达HTTP 200 provider envelope，但evidence binding被本地schema拒绝；没有accepted receipt、Journal或生产切换。当前代码另有互斥的audit-only rejected-attempt receipt合同和独立audit Journal，A股v2 Prompt只完成离线fixture验证，二者都不能追溯包装该旧canary。Bull/Bear provider模式要求显式typed recorder、稳定request ID、同source verifier，以及由一个显式绝对accepted锚点派生的canonical accepted/rejected/provider-invocation Journal family；invocation逻辑键不依赖调用方ID，网络前落`in_flight`并持跨进程锁至唯一终态。非canonical family、相对路径、Unicode/大小写/真实路径或物理别名、未知mode、伪recorder、换ID重发、冲突、未知in-flight或持久化失败均fail closed。三类readback只属`local-integrity-only`；跨主机/生产worker共享同一锚点尚未装配验证，accepted evidence、认证稳定性、quota/限流/成本、数据留存和生产可用性仍未验证。
- `shared/crontab.txt` 是仓库调度模板，不是已安装 cron。模板已移除显式旧A股调度；仍保留的wrapper由不可环境覆盖的kill switch阻断（退出码78），只能用于识别历史安装依赖与退役审计。最新 identity preflight 没有 apply paused TA cron；后续只能用 merge tool 原子暂停并证明零 TA job、non-TA 字节保留。UID/GID 987 与 immutable release 是部分 preflight，不是现役切换；current/front/token/tmpfiles/timer 都未据此晋级，`production_verified=false` 保持不变。
- Mini/Hermes webhook、file consumer 与 `RealSignalQueue` 在仓库合同层已退役并由 `tradingagent_mini_hermes_retirement` 阻止恢复；A股只保留`tradingagent.ashare.paper_broker.v1`的server-local模拟合同。`ASHARE_SIM_HERMES_ENABLED=0`和`ASHARE_SIM_WEBHOOK_ENABLED=0`仅是安装态清理墓碑，不代表服务器或Mini上的cron、env、process、port已经清零；这些仍需独立只读readback。
- `tradingagent_market_lane_governance`登记A股、CNFutures、Crypto三个长期worktree/branch/path owner，并要求三套模拟合同、外部测试合同和未来live adapter family互不重复且`live_enabled=false`。A股20交易日fixture loop与CNFutures闭环已是主线仓库合同；Crypto本批只登记本地fixture opening candidate，旧direct workflow/simulator/executor/shadow writer已退役。它们都不证明服务器调度、真实数据、Testnet或实盘API可用。
- `tradingagent_retired_noncore_markets`证明仓库合同中 US/PM/HK、旧多市场规则/退出 facade 与通用 Style/Evolution 执行面已物理删除，并以静态门禁阻止恢复；它不证明服务器安装态 cron、历史 runtime 或外部依赖已经清理。

## 仓库合同与机器门禁对照

| 对象 | 仓库观察 | YAML 门禁 | 当前可用范围 |
|---|---|---|---|
| TradingDatas V1 upstream query | TA不写TradingDatas；最新上游 190/12/178 metadata 与旧五项 one-shot 都不是 TA full active-set parity | `TARGET_CONTRACT` | 不代表 12-profile authenticated parity、TA credential、生产调度、历史 PIT 或交易通过 |
| TA TradingDatas V1 client + Evidence Gate | 实现与契约测试已进入仓库；兼容代码符号仍含`SharedSignalsV1*` | `CURRENT_VERIFIED / repository_contract / production=false` | 仅fixture/contract；不代表TradingDatas runtime |
| TA TradingDatas Bearer transport (`tradingagent_tradingdatas_bearer_transport`) | 最终transport只从`/run/secrets/tradingagent`受限token file注入header；精确绑定canonical authority/method/path与固定JSON header，远端只允许HTTPS；禁止明文env、调用方覆盖、secret回执/日志、认证重试和旧链fallback | `CURRENT_VERIFIED / repository_contract / production=false` | 仅消费侧文件/header合同；实际TA-scoped token、哈希注册和live认证readback仍未完成 |
| TA TradingDatas V1 integration-readiness probe | v2显式manifest；逐dataset filters/as-of/identity/event/budgets；provider-native rows、envelope source proof、bounded cursor、跨页守恒与same-observation双跑 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅fixture或另行授权的只读联调；current observation不是历史PIT，也不是生产或交易authority |
| TA TradingDatas catalog parity | review #23 合入的九 profile fixture 为190/9/181、5 ready/4 impaired；最新上游 metadata 为 production `c3232d0…`、catalog `v1-fcc1aaa39c20743e`、190/12/178、5 ready/7 impaired，新增三项partial/degraded且suspend stale/degraded | `TARGET_CONTRACT / repository_contract / production=false` | TA 12-profile authenticated parity `NOT RUN / BLOCKED`；catalog metadata不是parity，且不声明全12项profiles或fresh TA credential存在 |
| TA dedicated service identity candidate | tracked sysusers/front合同已合入；服务器preflight只创建UID/GID 987与immutable release，current/front/fresh token/tmpfiles未切，既有secret parent/leaf仍是metadata-only的`marketgraph` ownership | `TARGET_CONTRACT / repository_contract / production=false` | 不把identity preflight当完整cutover；credential freeze前不得置换叶，paused cron与零-holder证据前不得apply tmpfiles；失败后不得恢复旧credential/TA cron或8082 fallback |
| A股 `a7488e9` historical authenticated current-observation | formal 18082 one-shot 与精确幂等重放只在 `a7488e9` 冻结三件字节通过；3041只沪深主板，双创/北交所个股排除，行业/指数仅上下文 | `HISTORICAL_READ_ONLY / server_validated_non_authority_simulation_only / production=false` | 只证明当时单次真实只读观察；不得绑定到当前五项源码，专用TA token/worker/timer、历史PIT、分钟证据和模拟成交均未激活 |
| A股五项 committed observation runtime | 当前仓库合同逐 session 绑定snapshot/probe/observation receipt/membership ledger，并以transaction-complete作为唯一commit point，显式记录 observed/excluded denominator | `CURRENT_VERIFIED / repository_contract / production=false` | 不可补写旧 `a7488e9` state，也未获当前服务器读回；缺complete的半写状态不可消费，observation不是tradable/candidate/order authority |
| A股 forward-history + daily-only planner | 至少21个forward session；缺calendar continuity/adjustment仍blocked；无label horizons，`paper_trade_session=null` 且 abstain | `TARGET_CONTRACT / repository_contract / production=false` | 无预测、资金、订单、成交、对账或SampleJournal authority |
| A股 observation timer | unit/timer 字节仅作 disabled 静态候选；UID/GID preflight 不等于 fresh TA credential、12-profile parity 或 daily immutable manifest rollover | `TARGET_CONTRACT / architecture_target / production=false` | 当前仍未安装/未启用；不得用identity/release存在或静态manifest/as-of冒充日更 |
| 主板三层 Universe | policy/snapshots/zero-leakage 合同已进入仓库；环境宽度由内容寻址CoverageReceipt及外部verifier派生，过期/数量/双创聚合/authority缺口降级 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅模拟scope与cash+policy upper bound；真实coverage verifier、broker和ledger订单量均未证明 |
| Phase 1.5 行业 shadow 薄切片 | PIT taxonomy、成分、score方法/有效期、score/coverage receipts和独立proof绑定；动态 1 深研 + 2 观察 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅fixture研究聚焦；真实score verifier缺失；无个股、无position effect、无晋级资格 |
| 50k optimizer + plan binding | 可行池负责cash+policy上界；无默认account verifier复核账户；Champion score另绑定当前selection/artifact/model/spec与经独立port复核的数值PIT特征；plan再绑定T+1、cost、现金顺序、零股卖出与订单量 | `CURRENT_VERIFIED / repository_contract / production=false` | rank只排序、固定probe sizing；fixture proof不证明真实账户、Champion/feature registry或broker |
| 六维论点风险 authority | 显式人工policy、逐候选/持仓/pending detached proof与完整exposure-set receipt；复核六维cap、跨决策连续性、同symbol group identity、精确notional delta和最终map；嵌套proof不可借外层状态洗白 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅本地fixture；不证明生产行业/论点映射、真实pending book、外部签名或实盘上限科学性 |
| canonical account authority | 从当前simulated capital ledger head派生账户快照并绑定trade date、generation、execution lineage、现金、持仓与mark receipt；head或identity漂移即fail closed | `CURRENT_VERIFIED / repository_contract / production=false` | 仅本地模拟authority；不证明broker readback、生产账户或真实资金 |
| market evidence + execution clock | mark/quote绑定dataset/catalog/source receipt/lineage/calendar/capital context；显式fixture clock在sim submit与capital commit前分别重验freshness/session | `CURRENT_VERIFIED / repository_contract / production=false` | 本地hash不是签名；fixture verifier/clock不证明TradingDatas live、交易所行情或生产时间authority |
| model lifecycle/labels | metrics v2 由固定本地trust-root verifier重读完整artifact/receipt并绑定实现/标签/成本/窗口/horizon/regime/source receipts；负向动作持久锁存；A股ValidationPlan经无默认calendar verifier并冻结proof，SampleJournal/ops显式贯穿plan，CLI只加载外部内容寻址artifact且不自签，forward targets从同一会话authority派生 | `CURRENT_VERIFIED / repository_contract / production=false` | 只允许自动收紧；metrics proof是本地完整性hash而非签名/真实独立重算authority；生产calendar、真实market-truth和受信artifact registry仍缺；恢复、晋级和扩风险均需人工 |
| fixture paper day loop/CLI/store | `compose_paper_runtime`、fixture-only ports/账户proof、仓外CLI、原子RunBundle和Decision Ledger合同已进入仓库 | `CURRENT_VERIFIED / repository_contract / production=false` | 可执行但非authority；不是scheduler/live runtime，fixture不得写正式晋级样本 |
| capital-backed composition | canonical simulated account、current generation/lineage、人工选定Champion、逐副作用authority门、capital outbox、risk/execution/reconcile组合合同已进入仓库 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅test-only装配；无CLI、scheduler、live TradingDatas或真实paper sample |
| LLM provenance router + journals | 显式typed recorder只接受一个绝对accepted锚点及其确定性伴随路径，三类Journal端点构造后只读；逻辑内容键不依赖调用方ID，provider-invocation Journal在网络前落`in_flight`并于同一canonical family内持跨进程锁至唯一终态，accepted/rejected互斥结果分别写入物理隔离结果Journal；六个data/head端点互异，unknown mode、伪recorder、ID冲突、未知in-flight、文件身份或持久化失败均fail closed | `CURRENT_VERIFIED / repository_contract / production=false` | 仅local-integrity-only研究/审计；未验证跨主机共享锚点，不是provider attestation、外部durability、模型可用性或任何交易authority |
| DeepSeek HTTPS transport | 固定官方endpoint、显式凭据文件、系统TLS、禁代理/重定向/自动重试、严格JSON与互斥的accepted/rejected typed receipts；公开send/脱离Gateway的Adapter拒绝，wire path只接受Gateway验证后铸造、由进程内HMAC绑定关键字段的capability；fake opener合同通过，另有一次旧v1 schema-rejected真实canary | `CURRENT_VERIFIED / repository_contract / production=false` | evidence-only；默认关闭；accepted evidence、认证稳定性、quota、成本、延迟和生产激活均未验证 |
| Today 面板 | 只读reader与publisher相对路径一致 | repository contract / production=false | 活动根无当日投影时显示unavailable；不表示公开入口或生产激活 |
| 旧 A股 cron/wrappers | 仓库模板已删除调度；wrapper入口统一硬阻断 | repository contract / production=false | wrapper不可运行；安装态依赖仍需独立只读盘点 |
| Mini/Hermes 与 RealSignalQueue 退役 | 专用源码、网络发送器和Mini消费者已删除；旧配置truthy fail closed | `RETIRED_BLOCKED / repository_contract / production=false` | 仅证明仓库源码退役；不证明已安装服务器或Mini运行态已清理 |
| 三市场长期 lane 与 BrokerAdapter 边界 | worktree/branch/path owner及互异合同已登记，live统一关闭 | `CURRENT_VERIFIED / repository_contract / production=false` | 仅仓库治理门；不证明任一未来live adapter已实现或部署 |
| A股 market-kernel | server-local paper、T+1边界与20交易日fixture loop已进入仓库 | repository contract；本轮新增loop尚无服务器现役证明 | 不证明已连接券商、真实数据或现役 runtime 已切换 |
| CNFutures market-kernel | 独立期货 paper、会话/保证金/证据边界与fixture闭环已进入仓库 | repository contract；本轮新增loop尚无服务器现役证明 | 不证明 CTP/SimNow、真实规格或 live adapter 可用 |
| Crypto market-kernel | 10,000 USDT 本地fixture opening candidate、非权威paper receipt与独立Testnet/Live合同边界已进入仓库；旧direct执行已退役 | repository contract；冻结功能基线已完成network-disabled服务器旁路与已停止的loopback canary | 不证明current capital、7x24 scheduler、Binance Testnet、Live或现役runtime已连接 |

## 完整机器条目索引

下表逐项投影机器 YAML 的 `entry_id`。这里不复制完整 allowed/prohibited uses；发生冲突时以机器条目的更严格门禁为准。

| `entry_id` | 状态 / layer | 人工说明 |
|---|---|---|
| `sharedsignals_v1_query` | `TARGET_CONTRACT / repository_contract` | 保留的机器条目ID；最新上游12-active metadata不替代TA authenticated parity |
| `tradingagent_sharedsignals_v1_client` | `CURRENT_VERIFIED / repository_contract` | 保留的兼容机器条目ID；provider-native envelope consumer，不证明 live TradingDatas |
| `tradingagent_tradingdatas_bearer_transport` | `CURRENT_VERIFIED / repository_contract` | 受限TA token file到两个固定V1 endpoint的Bearer注入；实际token发放、注册和live认证readback仍未完成 |
| `tradingagent_sharedsignals_v1_integration_probe` | `CURRENT_VERIFIED / repository_contract` | 保留的兼容机器条目ID；v2 bounded-pagination、source-proof、identity守恒与same-observation只读接入门；曾由 `a7488e9` 使用历史 `marketgraph` 身份一次性运行，专用TA身份尚未运行 |
| `tradingagent_tradingdatas_catalog_parity` | `TARGET_CONTRACT / repository_contract` | 九profile fixture已合入但latest 12-profile TA parity仍NOT RUN/BLOCKED；catalog metadata不能替代认证双跑，profiles/credential未声明存在 |
| `tradingagent_dedicated_service_identity_candidate` | `TARGET_CONTRACT / repository_contract` | UID/GID 987与immutable release已preflight；current/front/fresh token/tmpfiles未切，仍须pause/zero-holder/freeze/publisher顺序 |
| `tradingagent_ashare_observation_runtime` | `CURRENT_VERIFIED / repository_contract` | 当前五项 committed observation 仓库合同；MarketGraph off；无当前服务器读回、历史PIT、执行副作用或持久调度 |
| `tradingagent_ashare_observation_a7488e9_historical_readback` | `HISTORICAL_READ_ONLY / server_validated_non_authority_simulation_only` | 只保存 `a7488e9` 三件 formal 18082 one-shot 历史证据；不得升级或绑定当前源码 |
| `tradingagent_ashare_observation_membership_ledger` | `TARGET_CONTRACT / repository_contract` | 新 fresh state 的五项 committed binding；无 label/trading authority |
| `tradingagent_ashare_prospective_history_readiness` | `TARGET_CONTRACT / repository_contract` | 21 session 最小覆盖；calendar continuity/adjustment 缺失仍 blocked |
| `tradingagent_ashare_daily_only_paper_planning` | `TARGET_CONTRACT / repository_contract` | `paper_trade_session=null` 的 deterministic abstain；无资金或执行副作用 |
| `tradingagent_ashare_observation_timer_candidate` | `TARGET_CONTRACT / architecture_target` | 当前未安装/未启用；UID/GID preflight不替代fresh token、12-profile parity与daily immutable manifest rollover |
| `tradingagent_mainboard_scope` | `CURRENT_VERIFIED / repository_contract` | 主板个股；双创/北交所等非主板指数与行业聚合仅环境参考，非主板个股禁止分析；覆盖 authority 需外部复核 |
| `tradingagent_small_account_optimizer` | `CURRENT_VERIFIED / repository_contract` | 50k、账户输入/proof绑定、整数股/零股卖出、费用与现金的模拟优化器；不证明真实账户 |
| `tradingagent_thesis_risk_authority` | `CURRENT_VERIFIED / repository_contract` | 行业/论点/原材料/政策事件/拥挤/模型家族六维风险完整性门；fixture policy/proof不可晋级 |
| `tradingagent_small_account_plan_binding` | `CURRENT_VERIFIED / repository_contract` | 计划绑定模拟账户输入/proof、T+1、cost policy与复算费用；fixture无broker authority |
| `tradingagent_canonical_account_authority` | `CURRENT_VERIFIED / repository_contract` | 当前simulated capital ledger head是优化器账户快照来源；漂移fail closed，不证明live broker或生产账户 |
| `tradingagent_champion_authority_binding` | `CURRENT_VERIFIED / repository_contract` | 当前selection/artifact/model/spec与数值PIT feature proof双重绑定；rank-only、固定probe；无生产registry verifier |
| `tradingagent_market_evidence_authority` | `CURRENT_VERIFIED / repository_contract` | mark/quote完整本地authority绑定；fixture hash不是签名或live市场readback |
| `tradingagent_trusted_execution_clock` | `CURRENT_VERIFIED / repository_contract` | 逐副作用fixture时钟重验quote；无默认/生产时钟 |
| `tradingagent_phase1_industry_shadow_slice` | `CURRENT_VERIFIED / repository_contract` | v2：1 深研 + 2 观察，score authority proof，仅 shadow |
| `tradingagent_llm_evidence` | `CURRENT_VERIFIED / repository_contract` | evidence-only sidecar；accepted与audit-only rejected receipt合同互斥；一次旧v1真实请求schema拒绝，无accepted evidence或durable sink |
| `tradingagent_llm_evidence_journal` | `CURRENT_VERIFIED / repository_contract` | accepted/rejected物理隔离结果Journal加独立provider-invocation仲裁Journal；本地CAS、hash-chain、`.head`完整性锚点和深层不可变readback；descriptor只作非权威结构校验，不重建typed receipt；不是外部密封、tamper-proof authority或生产durable sink |
| `tradingagent_llm_provenance_router` | `CURRENT_VERIFIED / repository_contract` | 显式recorder、canonical Journal family、稳定request ID、ID无关逻辑内容键、family内跨进程in-flight仲裁、互斥结果路由与终态精确重放；非canonical/相对/别名路径、unknown mode、未知in-flight或持久化失败fail closed；未验证跨主机共享锚点，无生产或交易authority |
| `tradingagent_deepseek_provider_config` | `CURRENT_VERIFIED / repository_contract` | 官方公开接口目标已核对；配置不读取密钥值，环境布尔值不能单独启用网络；任意模型映射仅为`fixture_only`；一次schema-rejected canary不证明accepted readback或认证稳定性 |
| `tradingagent_model_lifecycle` | `CURRENT_VERIFIED / repository_contract` | 只允许自动收紧，人工恢复/晋级 |
| `tradingagent_metrics_verification_authority` | `CURRENT_VERIFIED / repository_contract` | 固定本地implementation trust root并全字段复核；不等于签名或外部独立重算 |
| `tradingagent_trusted_evolution_clock` | `CURRENT_VERIFIED / repository_contract` | 仅接受显式冻结fixture时钟并绑定证据；无默认wall clock或生产调度时间authority |
| `tradingagent_scientific_validation_contract` | `CURRENT_VERIFIED / repository_contract` | 外部冻结plan artifact、calendar proof与forward target绑定存在；CLI不自签；不等于生产calendar、exit真值或统计实证完成 |
| `tradingagent_drift_runtime_binding` | `CURRENT_VERIFIED / repository_contract` | risk与network-closed simulation前重读持久latch；live broker副作用前门禁仍未验证 |
| `tradingagent_day_loop` | `CURRENT_VERIFIED / repository_contract` | 网络关闭的 fixture 编排，不是 scheduler |
| `tradingagent_paper_runtime_composition` | `CURRENT_VERIFIED / repository_contract` | 确定性本地 composition |
| `tradingagent_capital_backed_paper_runtime_composition` | `CURRENT_VERIFIED / repository_contract` | canonical simulated account、人工选定Champion、capital-backed risk/execution/reconcile与drift门禁的网络关闭组合；不是live runtime |
| `tradingagent_phase1_fixture_cli` | `CURRENT_VERIFIED / repository_contract` | 冻结 fixture 闭环，不写正式样本 |
| `tradingagent_opportunity_intelligence` | `CURRENT_VERIFIED / repository_contract` | PIT机会发现、状态迁移和append-only ledger的shadow合同；不证明机会有效或可交易 |
| `tradingagent_multihorizon_forecast` | `CURRENT_VERIFIED / repository_contract` | 未校准分位数/hazard与detached calibration研究artifact合同；不发布概率、不进决策 |
| `tradingagent_multistyle_router` | `CURRENT_VERIFIED / repository_contract` | 产业趋势/事件预期差/跨市场错配三袖套的去重与abstain shadow receipt；无资本authority |
| `tradingagent_deepseek_provider_transport` | `CURRENT_VERIFIED / repository_contract` | 默认关闭的严格官方HTTPS evidence transport；fake opener合同及一次旧v1 schema-rejected真实canary，不证明accepted evidence、生产部署或交易authority |
| `tradingagent_live_paper_scheduler` | `PLANNED_NOT_IMPLEMENTED / architecture_target` | live 自动模拟调度未实现 |
| `tradingagent_run_bundle_store` | `CURRENT_VERIFIED / repository_contract` | 本地不可变事件与恢复 |
| `tradingagent_decision_ledger` | `CURRENT_VERIFIED / repository_contract` | 成交、未成交、拒绝和观察四态账本 |
| `tradingagent_label_maturity` | `CURRENT_VERIFIED / repository_contract` | 外部验证 PIT 标签发布门 |
| `tradingagent_capital_authority` | `CURRENT_VERIFIED / repository_contract` | 模拟 A 股资本合同，不证明真实账户 |
| `tradingagent_execution_lineage` | `CURRENT_VERIFIED / repository_contract` | 模拟订单/成交/对账 lineage |
| `tradingagent_sample_journal` | `CURRENT_VERIFIED / repository_contract` | append-only 学习事实，不自动晋级 |
| `tradingagent_repository_cron_template` | `CURRENT_VERIFIED / repository_contract` | 只读调度设计；不代表已安装 |
| `tradingagent_mini_hermes_retirement` | `RETIRED_BLOCKED / repository_contract` | 专用源码禁止恢复；零值墓碑等待独立安装态readback，不证明生产已清理 |
| `tradingagent_market_lane_governance` | `CURRENT_VERIFIED / repository_contract` | A股、CNFutures、Crypto长期lane和互异BrokerAdapter合同；无live实现或部署证明 |
| `tradingagent_ashare_twenty_day_fixture_loop` | `CURRENT_VERIFIED / repository_contract` | 网络关闭的20交易日fixture自动模拟合同；不证明scheduler、真实数据或收益 |
| `tradingagent_cnfutures_fixture_closed_loop` | `CURRENT_VERIFIED / repository_contract` | 网络关闭的期货会话/保证金/费用/MTM/平仓/对账fixture合同；不是交易所级真值 |
| `tradingagent_crypto_fixture_auto_sim` | `CURRENT_VERIFIED / repository_contract` | generation 1本地fixture opening候选、非权威receipt与独立LLM sidecar；无current runtime/Testnet/Live |
| `tradingagent_crypto_execution_retirement` | `RETIRED_BLOCKED / repository_contract` | 旧Crypto direct workflow/simulator/executor/shadow writer与generic registry路径禁止恢复 |
| `tradingagent_retired_noncore_markets` | `RETIRED_BLOCKED / repository_contract` | US/PM/HK与旧共享市场执行语义已从仓库合同删除；安装态仍需独立readback |
| `tradingagent_installed_cron` | `CURRENT_VERIFIED / production_runtime_read_only_snapshot` | identity preflight未apply paused cron；旧TA条目只有在merge-tool原子pause并读回零TA job/non-TA保留后才算停用，pause失败后禁止恢复旧TA cron |
| `tradingagent_production_runtime` | `CURRENT_VERIFIED / production_runtime_read_only_snapshot` | UID/GID 987与immutable release是部分preflight；current/front/fresh token/tmpfiles/timer未切，不能称现役激活 |
| `tradingagent_server_sidecar_candidate` | `CURRENT_VERIFIED / server_validated_non_authority_simulation_only` | detached候选已在目标机通过测试与已停止的18787 loopback canary；现役service/cron/8787未变，无live数据或交易authority |
| `tradingagent_front` | `CURRENT_VERIFIED / repository_contract` | 只读模拟看板；`tradingagent.cc`仅作待验收的单用户认证入口，不写订单或资金、不允许匿名访问/API直出 |

## 阶段出口前的必需证据

1. 本地精确 diff、聚焦/全后端/前端检查、离线端到端、crash/restart 和独立 review；
2. TradingDatas 上游 handoff 冻结的 catalog version、dataset IDs、schema/query policy、auth、receipt authority，以及TA使用自身token/client得到的fresh runtime readback；
3. A股消费者同 `as_of` parity、V1 cutover、旧 import/URL/env/cron/front 引用清零与 runtime no-fallback 负例；
4. 生产market-evidence、Champion/feature registry、六维论点风险policy/exposure-set verifier、metrics重算与可信时钟authority接入并完成readback；本地fixture proof不得被复用为生产凭证；
5. 当前本地候选状态已写入机器YAML；冻结候选前仍须复核代码、测试、文档、YAML evidence和精确diff一致，任何后续变化继续在同一变更中对齐。

## 更新纪律

任何条目变更必须同时更新 YAML、相关契约/测试、`STATUS.md` 与退役清单。只有对应 layer 的新鲜 readback 才能改变 `production_verified`；本地测试、Mock、文档或 HTTP 200 均不能代替该证据。
