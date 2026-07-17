# System State Matrix

> 本文解释系统状态治理；当前能力状态的机器可读事实源是 `shared/governance/system_state_matrix.yaml`，旧链消费者与退役门的机器可读事实源是 `shared/governance/legacy_inventory.yaml`。本地候选、Git 主线、生产文件、生产 runtime、已安装 cron、真实数据与真实交易动作必须分别验证。当前YAML已把若干构件标为`CURRENT_VERIFIED`，但其`layer=local_isolated_candidate`且`production_verified=false`；这只证明对应本地allowed uses，不能静默提升为仓库主线或runtime。

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

- SharedSignals `/v1/catalog` 和 `/v1/query` 由上游唯一 writer/reviewer 负责；TA 只实现可配置 fixture/port consumer，不读取或修改 SharedSignals 仓，也不把 HTTP 200 当成 dataset 可用。
- A股资本、执行 lineage 与 SampleJournal 是仓库契约层当前能力；它们不证明生产 runtime、cron 或真实市场样本已验证。
- 主板 scope、Phase 1.5 行业 shadow 薄切片、OpportunityRadar/Ledger、多期限 forecast 合同、三风格 shadow router、LLM evidence与本地CAS journal、无密钥DeepSeek候选配置、默认关闭的DeepSeek HTTPS transport候选、V1 client、小资金 optimizer、六维论点风险authority、canonical simulated account authority、当前Champion/数值PIT特征绑定、mark/quote evidence authority、逐副作用trusted clock、fixture evolution clock、固定trust-root metrics verifier、authority-bound plan、negative-only evolution、automatic day loop、capital-backed paper composition、fixture CLI、RunBundle store、Decision Ledger和label maturity已在YAML中登记为`CURRENT_VERIFIED`的`local_isolated_candidate`。这不表示预测有效、概率已校准、阶段通过、Git主线、scheduler、live SS、真实paper session、真实DeepSeek调用或生产可用。coverage/industry score/account/thesis-risk/calendar/market evidence/Champion registry/feature/metrics/clock 的生产 verifier 均未接入；本地proof只证明完整输入绑定，不是外部签名、外部密封或真实市场/账户readback。行业薄切片只动态选择 1 个深研行业和 2 个观察行业；新增机会/预测/风格链只输出shadow审计，均不能改变 Champion、仓位、风险或订单。
- 当前 DeepSeek 候选接受两种精确类型：同时绑定request/outbound identity的冻结离线响应fixture，以及默认关闭、固定官方HTTPS地址、禁代理/重定向/自动重试的`DeepSeekHTTPTransport`。HTTP路径仅用本地fake opener验证，未读取真实credential、未发起真实请求。2026-07-16已核对官方公开base URL、V4 Flash/Pro模型ID、JSON与thinking说明；认证readback、真实canary、quota/限流/成本和数据留存仍未验证。
- `shared/crontab.txt` 是仓库调度模板，不是已安装 cron。模板已移除显式旧A股调度；仍保留的wrapper由不可环境覆盖的kill switch阻断（退出码78），只能用于识别历史安装依赖与退役审计。`/opt/investment/tradingagent`和安装态cron本轮不访问、不修改，状态保持historical/unverified。

## 本地候选与机器门禁对照

| 对象 | 本地工作树观察 | YAML 门禁 | 当前可用范围 |
|---|---|---|---|
| SS V1 upstream query | TA不写SS；等待上游唯一owner冻结 | `TARGET_CONTRACT` | 不代表SS runtime |
| TA SS V1 client + Evidence Gate | 实现与契约测试候选存在 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅fixture/contract；不代表SS runtime |
| 主板三层 Universe | 本地policy/snapshots/zero-leakage候选存在；环境宽度由内容寻址CoverageReceipt及外部verifier派生，过期/数量/双创聚合/authority缺口降级 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅模拟scope与cash+policy upper bound；真实coverage verifier、broker和ledger订单量均未证明 |
| Phase 1.5 行业 shadow 薄切片 | PIT taxonomy、成分、score方法/有效期、score/coverage receipts和独立proof绑定；动态 1 深研 + 2 观察 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅fixture研究聚焦；真实score verifier缺失；无个股、无position effect、无晋级资格 |
| 50k optimizer + plan binding | 可行池负责cash+policy上界；无默认account verifier复核账户；Champion score另绑定当前selection/artifact/model/spec与经独立port复核的数值PIT特征；plan再绑定T+1、cost、现金顺序、零股卖出与订单量 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | rank只排序、固定probe sizing；fixture proof不证明真实账户、Champion/feature registry或broker |
| 六维论点风险 authority | 显式人工policy、逐候选/持仓/pending detached proof与完整exposure-set receipt；复核六维cap、跨决策连续性、同symbol group identity、精确notional delta和最终map；嵌套proof不可借外层状态洗白 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅本地fixture；不证明生产行业/论点映射、真实pending book、外部签名或实盘上限科学性 |
| canonical account authority | 从当前simulated capital ledger head派生账户快照并绑定trade date、generation、execution lineage、现金、持仓与mark receipt；head或identity漂移即fail closed | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅本地模拟authority；不证明broker readback、生产账户或真实资金 |
| market evidence + execution clock | mark/quote绑定dataset/catalog/source receipt/lineage/calendar/capital context；显式fixture clock在sim submit与capital commit前分别重验freshness/session | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 本地hash不是签名；fixture verifier/clock不证明SS live、交易所行情或生产时间authority |
| model lifecycle/labels | metrics v2 由固定本地trust-root verifier重读完整artifact/receipt并绑定实现/标签/成本/窗口/horizon/regime/source receipts；负向动作持久锁存；A股ValidationPlan经无默认calendar verifier并冻结proof，SampleJournal/ops显式贯穿plan，CLI只加载外部内容寻址artifact且不自签，forward targets从同一会话authority派生 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 只允许自动收紧；metrics proof是本地完整性hash而非签名/真实独立重算authority；生产calendar、真实market-truth和受信artifact registry仍缺；恢复、晋级和扩风险均需人工 |
| fixture paper day loop/CLI/store | `compose_paper_runtime`、fixture-only ports/账户proof、仓外CLI、原子RunBundle和Decision Ledger候选存在 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 可执行但非authority；不是scheduler/live runtime，fixture不得写正式晋级样本 |
| capital-backed composition | canonical simulated account、current generation/lineage、人工选定Champion、逐副作用authority门、capital outbox、risk/execution/reconcile组合候选存在 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | 仅test-only装配；无CLI、scheduler、live SS或真实paper sample |
| DeepSeek HTTPS transport | 固定官方endpoint、显式凭据文件、系统TLS、禁代理/重定向/自动重试、严格JSON与typed HTTP receipt；公开send/脱离Gateway的Adapter拒绝，wire path只接受Gateway验证后铸造、由进程内HMAC绑定关键字段的capability；仅fake opener验收 | `CURRENT_VERIFIED / local_isolated_candidate / production=false` | evidence-only；默认关闭；真实credential、认证readback、quota、成本、延迟和真实provider请求均未验证 |
| Today 面板 | 只读reader与publisher相对路径一致 | front/local candidate only | 活动根无当日投影时显示unavailable |
| 旧 A股 cron/wrappers | 仓库模板已删除调度；wrapper入口统一硬阻断 | repository contract / production=false | wrapper不可运行；安装态依赖仍需独立只读盘点 |

## 完整机器条目索引

下表逐项投影机器 YAML 的 `entry_id`。这里不复制完整 allowed/prohibited uses；发生冲突时以机器条目的更严格门禁为准。

| `entry_id` | 状态 / layer | 人工说明 |
|---|---|---|
| `sharedsignals_v1_query` | `TARGET_CONTRACT / local_isolated_candidate` | 上游 SS owner 负责；TA 仅等待冻结合同 |
| `tradingagent_sharedsignals_v1_client` | `CURRENT_VERIFIED / local_isolated_candidate` | mock-first V1 consumer，不证明 live SS |
| `tradingagent_mainboard_scope` | `CURRENT_VERIFIED / local_isolated_candidate` | 主板个股；双创指数与行业聚合仅环境参考；覆盖 authority 需外部复核 |
| `tradingagent_small_account_optimizer` | `CURRENT_VERIFIED / local_isolated_candidate` | 50k、账户输入/proof绑定、整数股/零股卖出、费用与现金的模拟优化器；不证明真实账户 |
| `tradingagent_thesis_risk_authority` | `CURRENT_VERIFIED / local_isolated_candidate` | 行业/论点/原材料/政策事件/拥挤/模型家族六维风险完整性门；fixture policy/proof不可晋级 |
| `tradingagent_small_account_plan_binding` | `CURRENT_VERIFIED / local_isolated_candidate` | 计划绑定模拟账户输入/proof、T+1、cost policy与复算费用；fixture无broker authority |
| `tradingagent_canonical_account_authority` | `CURRENT_VERIFIED / local_isolated_candidate` | 当前simulated capital ledger head是优化器账户快照来源；漂移fail closed，不证明live broker或生产账户 |
| `tradingagent_champion_authority_binding` | `CURRENT_VERIFIED / local_isolated_candidate` | 当前selection/artifact/model/spec与数值PIT feature proof双重绑定；rank-only、固定probe；无生产registry verifier |
| `tradingagent_market_evidence_authority` | `CURRENT_VERIFIED / local_isolated_candidate` | mark/quote完整本地authority绑定；fixture hash不是签名或live市场readback |
| `tradingagent_trusted_execution_clock` | `CURRENT_VERIFIED / local_isolated_candidate` | 逐副作用fixture时钟重验quote；无默认/生产时钟 |
| `tradingagent_phase1_industry_shadow_slice` | `CURRENT_VERIFIED / local_isolated_candidate` | v2：1 深研 + 2 观察，score authority proof，仅 shadow |
| `tradingagent_llm_evidence` | `CURRENT_VERIFIED / local_isolated_candidate` | evidence-only sidecar；有离线fixture与严格HTTPS候选的typed transport receipt合同，无真实DeepSeek调用或durable sink |
| `tradingagent_llm_evidence_journal` | `CURRENT_VERIFIED / local_isolated_candidate` | 本地CAS、hash-chain与`.head`完整性锚点；不是外部密封、tamper-proof authority或生产durable sink |
| `tradingagent_deepseek_provider_config` | `CURRENT_VERIFIED / local_isolated_candidate` | 官方公开接口目标已核对；配置不读取密钥值，环境布尔值不能单独启用网络；任意模型映射仅为`fixture_only`，认证readback/canary仍不存在 |
| `tradingagent_model_lifecycle` | `CURRENT_VERIFIED / local_isolated_candidate` | 只允许自动收紧，人工恢复/晋级 |
| `tradingagent_metrics_verification_authority` | `CURRENT_VERIFIED / local_isolated_candidate` | 固定本地implementation trust root并全字段复核；不等于签名或外部独立重算 |
| `tradingagent_trusted_evolution_clock` | `CURRENT_VERIFIED / local_isolated_candidate` | 仅接受显式冻结fixture时钟并绑定证据；无默认wall clock或生产调度时间authority |
| `tradingagent_scientific_validation_contract` | `CURRENT_VERIFIED / local_isolated_candidate` | 外部冻结plan artifact、calendar proof与forward target绑定存在；CLI不自签；不等于生产calendar、exit真值或统计实证完成 |
| `tradingagent_drift_runtime_binding` | `CURRENT_VERIFIED / local_isolated_candidate` | risk与network-closed simulation前重读持久latch；live broker副作用前门禁仍未验证 |
| `tradingagent_day_loop` | `CURRENT_VERIFIED / local_isolated_candidate` | 网络关闭的 fixture 编排，不是 scheduler |
| `tradingagent_paper_runtime_composition` | `CURRENT_VERIFIED / local_isolated_candidate` | 确定性本地 composition |
| `tradingagent_capital_backed_paper_runtime_composition` | `CURRENT_VERIFIED / local_isolated_candidate` | canonical simulated account、人工选定Champion、capital-backed risk/execution/reconcile与drift门禁的网络关闭组合；不是live runtime |
| `tradingagent_phase1_fixture_cli` | `CURRENT_VERIFIED / local_isolated_candidate` | 冻结 fixture 闭环，不写正式样本 |
| `tradingagent_opportunity_intelligence` | `CURRENT_VERIFIED / local_isolated_candidate` | PIT机会发现、状态迁移和append-only ledger的shadow合同；不证明机会有效或可交易 |
| `tradingagent_multihorizon_forecast` | `CURRENT_VERIFIED / local_isolated_candidate` | 未校准分位数/hazard与detached calibration研究artifact合同；不发布概率、不进决策 |
| `tradingagent_multistyle_router` | `CURRENT_VERIFIED / local_isolated_candidate` | 产业趋势/事件预期差/跨市场错配三袖套的去重与abstain shadow receipt；无资本authority |
| `tradingagent_deepseek_provider_transport` | `CURRENT_VERIFIED / local_isolated_candidate` | 默认关闭的严格官方HTTPS evidence transport；仅本地fake opener验收，无真实调用、生产部署或交易authority |
| `tradingagent_live_paper_scheduler` | `PLANNED_NOT_IMPLEMENTED / architecture_target` | live 自动模拟调度未实现 |
| `tradingagent_run_bundle_store` | `CURRENT_VERIFIED / local_isolated_candidate` | 本地不可变事件与恢复 |
| `tradingagent_decision_ledger` | `CURRENT_VERIFIED / local_isolated_candidate` | 成交、未成交、拒绝和观察四态账本 |
| `tradingagent_label_maturity` | `CURRENT_VERIFIED / local_isolated_candidate` | 外部验证 PIT 标签发布门 |
| `tradingagent_capital_authority` | `CURRENT_VERIFIED / repository_contract` | 模拟 A 股资本合同，不证明真实账户 |
| `tradingagent_execution_lineage` | `CURRENT_VERIFIED / repository_contract` | 模拟订单/成交/对账 lineage |
| `tradingagent_sample_journal` | `CURRENT_VERIFIED / repository_contract` | append-only 学习事实，不自动晋级 |
| `tradingagent_repository_cron_template` | `CURRENT_VERIFIED / repository_contract` | 只读调度设计；不代表已安装 |
| `tradingagent_installed_cron` | `HISTORICAL_READ_ONLY / production_runtime_unverified_this_turn` | 本轮未 readback |
| `tradingagent_production_runtime` | `HISTORICAL_READ_ONLY / production_runtime_unverified_this_turn` | 本轮未 readback、未写入 |
| `tradingagent_front` | `CURRENT_VERIFIED / repository_contract` | 只读模拟看板；`tradingagent.cc`仅作待验收的单用户认证入口，不写订单或资金、不允许匿名访问/API直出 |

## 阶段出口前的必需证据

1. 本地精确 diff、聚焦/全后端/前端检查、离线端到端、crash/restart 和独立 review；
2. SS 上游冻结的 catalog version、dataset IDs、auth、receipt authority 和真实 runtime readback；
3. A股消费者同 `as_of` parity、V1 cutover、旧 import/URL/env/cron/front 引用清零与 runtime no-fallback 负例；
4. 生产market-evidence、Champion/feature registry、六维论点风险policy/exposure-set verifier、metrics重算与可信时钟authority接入并完成readback；本地fixture proof不得被复用为生产凭证；
5. 当前本地候选状态已写入机器YAML；冻结候选前仍须复核代码、测试、文档、YAML evidence和精确diff一致，任何后续变化继续在同一变更中对齐。

## 更新纪律

任何条目变更必须同时更新 YAML、相关契约/测试、`STATUS.md` 与退役清单。只有对应 layer 的新鲜 readback 才能改变 `production_verified`；本地测试、Mock、文档或 HTTP 200 均不能代替该证据。
