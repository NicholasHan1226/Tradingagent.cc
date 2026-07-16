# TradingAgent 当前状态

> 最后更新：2026-07-16 CST。本文件只记录当前工作树证据、阻塞和下一门禁；长期规则见 [AGENTS.md](AGENTS.md)，运行命令见 [docs/operations.md](docs/operations.md)。旧提交、旧生产快照和作废候选从 Git 历史审计，不在现役状态文件重复维护。

## 当前结论：本地候选完成，隔离分支发布准备中

当前唯一开发位置是 `TradingAgent/.worktrees/ta-v1-data-client`，分支 `codex/ta-v1-data-client`。本地候选门已经关闭；当前发布授权只允许把同一候选提交并推送到该隔离分支。它不是 Phase 1 通过、Git 主线、SharedSignals live API、生产文件/runtime、已安装 scheduler/cron、真实模拟盘样本或真实交易完成证明。

- `REAL_TRADING_ENABLED=false`；候选分支提交/推送必须在秘密扫描与发布前检查通过后执行。仍未授权 merge/main、deploy、apply cron、发邮件、连接 broker、操作 GUI 或真实交易；生产密钥与服务器变更另设门禁。
- 产品边界已确认为 Nicholas 个人内部使用。`tradingagent.cc`保留为个人远程访问入口，但必须由Cloudflare Access或等价单用户认证保护；API继续只监听localhost，禁止匿名公网访问或API直出。本轮未修改DNS、Tunnel、Pages或Access policy，现网权限状态仍未验证。
- SharedSignals 完全由上游唯一 writer/reviewer 管理。TA 不读取或修改 SS 仓，只消费显式 fixture/port 的 `GET /v1/catalog` 与 `POST /v1/query` 合同。
- 本线程的交付目标是 TradingAgent 本地候选，不包含 SharedSignals 服务端重构、测试、集成或验收；任何把“SS 与 TA 重构”并列为本线程 Goal 的旧表述均以本条 ownership correction 为准。
- TA ownership 门禁已进一步收紧：本仓测试不得定位/导入/执行兄弟仓 reader、API server、SQLite 私有函数或复刻上游 HTTP server；current-v1 consumer 与候选清单不得依赖旧 reader、旧 runtime gate 或专用 SS 路由。原跨仓 edge/server 测试已从 TA 验收面清除，SS 服务端行为由上游自己的验收线负责。
- HTTP 200 不能覆盖逐 dataset 的 `state/degraded/freshness/quality/lineage/receipt` 失败；V1 QueryRequest 已要求 `schema_major`，`order` 省略时不发送并由 registry 默认排序。impaired dataset 可如实返回 null source proof，TA 不补造且 Evidence Gate fail closed；禁止 Tushare、SS SQLite、旧状态/数据专用端点和缓存数据 fallback。
- 机器状态见 `shared/governance/system_state_matrix.yaml`；其中 local candidate 的 `CURRENT_VERIFIED` 只在明确 allowed uses 内成立，全部 `production_verified=false`。

## 本地候选能力

1. **SS V1 client 与证据门**：provider-neutral catalog/query、显式 base URL/catalog/dataset/access policy、完整 envelope、分页/缓存身份与 fail-closed 负例；当前只有 fixture/合同证据。
2. **主板三层 Universe**：主板普通 A 股是唯一可进入个股分析、候选、预测、影子、仓位、订单、成交和持仓的 scope。创业板/科创板指数及行业聚合只作 `context_only` 环境证据。
3. **CoverageReceipt**：行业宽度由内容寻址 taxonomy、PIT membership、板块/行业 expected-vs-observed、双创环境对象和来源 generation/receipt/lineage 派生；构造与消费均要求无默认实现的外部 verifier 复核 denominator，调用方不能自报 full-market，缺失/过期/异常/未验证只可拒绝或 degraded。
4. **Phase 1.5 行业 shadow v2**：动态选择 1 个深研行业和 2 个观察行业，绑定 PIT taxonomy、score method/version/validity、score/coverage receipts、内容 hash 和独立 authority proof；不输出个股，不改变 Champion、仓位、风险、订单或晋级状态。当前只有 fixture verifier。
5. **50,000 CNY 小账户决策与论点风险**：唯一A股policy定义15%单票、90% gross、最多8仓、最低经济订单2,000 CNY和无交易区1,000 CNY。optimizer强制无默认`AccountAuthorityVerifier`，proof绑定完整账户内容、verifier身份与有效期；当前只有不可晋级fixture verifier，所以这里只证明输入/proof绑定，不证明真实账户、券商持仓或可卖数量。新增`ThesisRiskRuntimeAuthority`按行业、投资论点、原材料、政策/事件、拥挤、模型家族六维绑定显式人工policy、逐成员detached proof和完整候选/持仓/pending exposure-set receipt；运行时不能自签、漏记pending或跨决策重置，同一symbol的candidate/position/pending也不能改换group规避cap。只有open/increase受上限阻断，合法reduce/exit保留。买入为100股整数倍；卖出只允许100股整数倍、完整零股余额或全部退出且受T+1约束。canonical非空持仓mark与非空执行quote现强制嵌入完整`MarketEvidenceAuthority`，绑定dataset/catalog/source receipt/lineage、calendar、capital generation、execution lineage与时点；当前唯一具体verifier明确`production_eligible=false`，hash只证明本地完整性。day loop另行把每笔六维group绑定回权威exposure receipt，并复算论点暴露、佣金、过户费和卖出印花税。
6. **冻结 Champion**：第一阶段只有 `uncalibrated_deterministic_rank_score` 与现金基线。score receipt同时绑定当前人工selection manifest、artifact SHA、model ID/version、冻结spec和经独立port复核的数值PIT feature snapshot；future/LLM/过早或调用方自证feature拒绝。rank只排序，新仓保持与rank无关的固定probe；不声称概率、正期望或收益保证。生产Champion registry与数值feature authority verifier尚未接入。
7. **自动模拟日候选**：网络关闭的 `FrozenFixtureStagePort`、authority-bound plan、sim OMS、reconcile、Decision Ledger、RunBundle event store/publisher 和仓外 fixture CLI 支持幂等 replay。业务 receipt 排除本机绝对 Journal 路径并使用相对 publisher-root 的稳定 artifact 定位，因此相同输入跨不同输出根得到相同 run/bundle identity 与 artifact bytes；CLI 顶层仍返回可操作的绝对路径。fixture 永不进入正式 SampleJournal 或晋级证据。
8. **模型与科学治理**：`ValidationPlan`除实验预算/PBO/DSR/OOS外，强制无默认calendar verifier、预测前冻结的detached proof与完整交易会话；SampleJournal及A股label/sample ops已显式贯穿该计划，CLI要求`--validation-plan-path`加载预先生成且内容寻址的artifact，不在运行时调用verifier或铸造proof。A股`close/1d/3d/5d` target从同一authority派生、调用方不一致即拒绝、缺日线不顺延。metrics verifier现固定本地implementation trust root，重读canonical artifact/receipt并复核全部label/cost/source/window/horizon/regime/journal/model/sample-count绑定；proof仍只是本地完整性hash，不是签名或真实独立重算authority。生产calendar、受信artifact registry、真实exit/总回报/公司行动真值和统计实证仍未完成。
9. **持久 drift、恢复顺序与执行TOCTOU约束**：自动化只允许隔离、reduce-only、stop-new-risk或require-review。最新latch与Champion authority在每次risk评估及网络关闭的模拟副作用前重读；capital commit在时钟校验后、账务提交前还会做最终重读。显式`TrustedExecutionClock`在`sim_submit`与`capital_commit`前分别重验quote freshness/session，并以不截断的ISO时点强制`quote <= submit <= fill/terminal <= commit <= reconcile`。模拟fill使用submit副作用时间而不是较早quote时间；commit时钟倒退、跨交易日或quote失效会保留坏reading、把terminal停在最后合法时点、释放预约且不提交capital账务，并产生可严格对账的`not_committed`失败回执。回执固化market session、available/data-through与统一30秒TTL；日循环和对账按生产器相同优先级复核唯一失败原因、精确terminal、零成交、完整残量、无fill/commit ID及释放证明。零成交释放还要求订单cash/exposure等于canonical完整剩余预约，首次释放服从effect guard；精确release event在其事件前缀中必须立即得到terminal与cash/exposure/margin全零，部分释放、后补归零、legacy别名或terminal fill冒充release均阻断，同一reference只可幂等恢复既有终态event。崩溃恢复先校验pending outbox/完整receipt seed，并仅在canonical ledger返回对应commit的`idempotent=true`时补settlement；intent已落盘但commit未发生时不会绕过最新收紧门。当前仅有冻结fixture clock；生产time authority、原子化外部authority+commit和未来live broker副作用门禁仍未验证。
10. **影子机会、预测与三风格合同**：`OpportunityRadar`以外部复核的PIT coverage denominator扫描主板，`OpportunityBatch`绑定完整扫描集合，OpportunityLedger按CAS/hash-chain保存不可变状态迁移并拒绝伪造/回退/无新证据更新；多期限合同输出`m30/m60/close/1d/3d/5d`未校准quantile/hazard，detached calibration artifact必须回绑同一forecast/PIT/model/OOS proof；三风格router仅组合`industry_trend/event_surprise/cross_market_dislocation`的去重证据并可abstain。三者都是shadow-only，不证明预测有效或可发布概率，且由静态依赖门禁止进入Champion、optimizer、risk或execution。
11. **LLM evidence sidecar**：固定Prompt、`untrusted_artifact_data`、内容寻址source span、PIT、source proof/verifier、全树输入/输出敏感数据门和显式中英文提示注入模式负例门已形成离线候选；typed provider receipt绑定request、source proof、transport material、实际provider outbound/response和标准化evidence hash。成功证据可写入带显式head CAS、hash-chain、原子本地`.head`锚点和readback验证的shadow journal；run ID由receipt派生，receipt swap/replay/partial tail/symlink/anchor不一致均拒绝。该本地锚点不是外部密封或tamper-proof authority；真实DeepSeek transport、生产verifier、durable sink和增量价值均未验证，LLM无交易权限。
12. **只读前端**：Today/market context 只读本地安全投影；旧机会漏斗文件只标为冻结法证历史，不能提升signals/risk readiness、heartbeat或当前持仓/PnL归因；无V1 authority的A股legacy pending queue行不进入当前signals。证据缺失显示 unavailable/degraded，不提供任何写资金、队列、订单、邮件或回调能力。

## 明确尚未实现

以下条目在机器状态中仍是 `PLANNED_NOT_IMPLEMENTED`，不能被文档、旧代码或界面描述成当前能力：

- 真实 DeepSeek provider transport；
- 真实 SS V1 驱动的 live paper scheduler。

机会雷达/Ledger、多期限forecast和三风格router的**本地shadow合同**已经存在，但“实证有效、概率校准可发布、统一50k动态风格预算或接入当前订单链”仍未实现，不能借`CURRENT_VERIFIED/local_isolated_candidate`措辞升级。

本地calendar/forward-target绑定、外部计划artifact门与network-closed simulation前drift重读已关闭对应的本地合同层断点，但不能向上推断生产完成。live paper前仍须接入并readback真实calendar authority、受信计划artifact registry与市场标签证据，并在长驻scheduler/真实broker每个外部副作用前复核最新drift authority。

旧四风格、exploration/exploitation、旧 reader/专用端点、screening/benchmark、A股 wrapper/cron、旧 opportunity funnel writer 和旧 review/runtime 路径分为三类：A股 current-v1 不消费这些路径；非 A 股或人工法证路径仍是 active-compatibility；旧 A股 wrapper 与funnel writer均hard-blocked（退出码 78）且从仓库cron模板移除。旧funnel读侧仅保留冻结历史，消费者与安装态依赖尚未全部清零，不能标记物理retired，也不能进入 V1 candidate。

## 当前验证状态

- 当前后端收集集合为`179`个`tests/test_*.py`、`2923 tests`。发布前单进程完整回归在最终后端代码上`2923 passed`、0 failed、0 skipped，pytest时间`936.65s`；V1唯一候选清单在最终文档/产品边界更新后再次复跑为`1380 passed in 27.14s`，DeepSeek/LLM/架构专项`176 passed`并另有最终Router专项`52 passed`，均为0 failed。测试只证明本地候选，不代表生产runtime。
- 前端只读面：43 个测试文件、`276 passed`；`npm run lint` 与 `npm run build:all` 通过。新增单用户部署负例证明服务拒绝非loopback监听与`*` CORS。另以本地`vite preview`和headed Playwright真实渲染检查总览空态、状态标签、布局与文字溢出，检查后关闭浏览器/服务并清理临时截图。
- 当前变更/未跟踪 Python 项共157个，其中154个现存文件全部通过Ruff check与Ruff format check，另3个是明确删除的旧文件；27个变更shell文件通过`bash -n`，`git diff --check`通过。credential-shaped扫描只命中显式合成的负例canary，`.env.example`中的`DEEPSEEK_API_KEY`保持空值，未发现真实凭据入仓。
- 仓外 CLI 三次实跑均 `completed`：同输入跨两个不同真实 `/private/tmp` 输出根的 run ID、bundle SHA 和 artifact bytes 相同；同根第二次 `idempotent=true` 且 `transport_calls=[]`。本轮 run ID 为`ashare-paper-day-7c1b170499742adff759247b992ceb00`，bundle SHA-256 为`03c690274af36dd8e72196a6b831395401f0dd5e1c853e5bf09d9825f0877027`，但该产物仍是`non_authority`、`production_verified=false`。
- 四轮独立科学复核累计发现并关闭：commit后崩溃恢复P1、时间链/失败原因缺口、跨订单预约归属P1、“半额release仍被对账成功”的资金冻结P1，以及重新签名后把同一股票改换六维风险组规避cap的P1。当前authority、optimizer与day loop都拒绝同symbol group重分类；day-loop对有效重签后的proof、final map和decision mirror也独立fail closed，嵌套fixture proof不能用外层非晋级标记掩盖。最新专项复核无P0，确认的group-binding P1已关闭；论点风险/optimizer/stage/day-loop/composition/架构九文件专项`255 passed in 6.97s`。本地hash仍是进程内完整性绑定，不是外部密码学签名；生产trust root继续列为阻塞。
- SS live、真实 paper session、生产 calendar/account/market-evidence/Champion-feature/metrics/time verifier、受信 ValidationPlan registry、生产 scheduler/cron、生产 runtime、真实市场样本、真实 DeepSeek transport 和 live broker 外部副作用门禁未验证。会话中曾暴露的 DeepSeek credential 未被TA保存或调用，但供应商侧 revoke/rotate 与新凭据 readback 尚未验证，继续作为外部阻塞。

## 第一阶段出口门禁

1. 本地架构重构候选的精确diff、完整后端、最终候选清单、前端lint/test/build/真实渲染、变更Python静态检查、shell语法、离线CLI三跑、文档对账和独立安全/科学复核均已完成；当前用户授权只扩展到隔离候选分支的commit/push，不授权merge/main、deploy、scheduler、live SS、真实paper或实盘；
2. 等待 SS 上游冻结 catalog version、dataset IDs、auth、receipt authority 与 runtime readback，再做同 `as_of` parity；
3. 按消费者分批切 V1；每批同时删除旧 import、URL、env、调度、测试和文档引用，并保留 runtime no-fallback 负例，不建立长期双轨；
4. 用真实 SS V1 与新鲜 50,000 CNY 模拟 authority 连续运行 20 个交易日，要求 0 未来数据、0 同 bar 成交、0 重复订单/成交、0 scope 泄漏、0 旧链 fallback、0 未解释账务差异；
5. 再积累 60–120 交易日冻结 OOS 与多状态样本，执行真实 purged/nested walk-forward、PBO/DSR、成本/未成交、尾部和校准/排序验证；
6. 月收益 20% 只报告达到概率、负月概率、尾部损失和风险毁灭概率，不是 PASS、交易频率或强迫满仓条件；任何晋级、恢复/扩风险或 live transition 仍需 Nicholas 单独批准。
