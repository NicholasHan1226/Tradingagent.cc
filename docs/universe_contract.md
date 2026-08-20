# A股 Universe Contract V1

契约 ID：`tradingagent.universe_scope.v1`

```yaml
contract_version: 1
implementation_state: repository_contract
effective_from: null
production_verified: false
supersedes: null
policy_code: shared/universe/policy.py
snapshot_code: shared/universe/snapshots.py
tests:
  - tests/test_mainboard_scope_policy.py
  - tests/test_three_universe_snapshots.py
```

上述状态表示契约已进入 Git 主线仓库合同，不表示服务器现役 runtime、生产 scheduler 或真实账户已按该契约运行。机器状态继续保持 `production_verified=false`；当前只允许 fixture/离线验证与明确标注的非权威旁路复现。

运行组合入口只接受精确类型 `CanonicalMainboardScopePolicy`。该类型无实例可变状态，
其 stage-neutral `ComponentIdentity.artifact_sha256` 由冻结 policy manifest 的 canonical JSON
计算；普通 duck-typed 对象、任意 callable 和 subclass 均不得自认证。离线 fixture CLI
也必须逐字段复核自身声明的 scope identity 与这份 canonical identity 完全一致，不能自行
提供另一份主板判定逻辑或任意 artifact SHA。

## 三层 Universe

1. `MarketContextUniverseSnapshot`：允许全A股指数、创业板指、科创50及全市场行业聚合；所有非个股环境对象必须标记 `context_only=true`，且不得携带可进入订单链的 security identity。`coverage_scope` 不接受调用者输入，只能由已校验的 `CoverageReceipt` 及本次实际环境对象共同派生。
2. `AccountTradableUniverseSnapshot`：只允许沪深主板普通A股；300/301/688/689、北京证券交易所、B股、基金、ETF、指数及未知板块个股均拒绝。
3. `SmallCapitalFeasibleUniverseSnapshot`：在模拟scope池上继续应用不超过 50,000 CNY 的cash+policy upper bound、100股整手、单票15%、费用、滑点、流动性和最低经济订单约束。当前候选不含真实持仓/可卖数量/ledger proof，不能直接解释为订单量。

后层只能增加约束，不能把前层拒绝对象重新升级为可交易标的。

## 动态证券状态与滚动成员

证券主数据是按决策时点生效的动态快照，不是永远固定且必须同时合格的名单。
上市不足 30 日、实际退市、停牌、风险警示或主数据缺失都必须按证券记录稳定的
`reason_code`，但单个证券的拒绝不得阻塞其它证券的运行。运行时应从 reviewed
source snapshot 派生当前 `rolling_active_partition`：只有本窗口逐股通过的成员才
进入模拟；新股进入 `pending` 并记录 `listed_on`/`eligible_after`；退市或其它失效
成员从下一窗口移出，同时保留历史事件和排除记录。不得用其它证券静默替换缺失成员。

严格的全量集合只用于覆盖率、审计或明确的“全量覆盖”声明，不得作为当前模拟交易
启动的总门禁。对已纳入 partition 的股票，行情 receipt、时间窗、分页、lineage 和
运行回读仍必须逐股严格完整；一批局部数据不完整只阻断该股票或该 shard。

## CoverageReceipt：行业宽度的唯一覆盖证明

`tradingagent.market_context_coverage.v1` 是内容寻址、冻结 dataclass。调用者不能再传
`coverage_scope=full_market` 自行声明覆盖完整。receipt 至少绑定：

```yaml
as_of:
taxonomy_id:
taxonomy_version:
taxonomy_sector_count:
membership_effective_at:
membership_available_at:
valid_until:
source_generation:
source_receipt_id:
source_lineage: []
source_sha256:
source_authority_status: external_verified | external_verification_required
source_authority_verifier_id:
source_authority_proof_id:
source_authority_verified_at:
board_counts:
  - dimension_id: mainboard | chinext | star | beijing
    expected_count:
    observed_count:
    coverage_ratio:
sector_counts:
  - dimension_id: canonical-sector-id
    expected_count:
    observed_count:
    coverage_ratio:
board_coverage_ratio:
sector_coverage_ratio:
coverage_ratio:
coverage_scope: derived-only
reason_codes: []
receipt_sha256:
```

`source_sha256` 绑定精确 board/sector count rows；`receipt_sha256` 再绑定 taxonomy、PIT
时钟、分母、source generation/receipt/lineage、外部 authority proof、派生比例和原因。MarketContext 快照绑定
`coverage_receipt_sha256`，并重算 receipt 内容、count rows、比例、scope 和原因；修改 dataclass
字段后重签 snapshot 不能洗白失配的 receipt。

内容 hash 只能证明调用方提交的 bytes 未变化，不能证明 expected/observed 分母真实。构造 receipt
和消费 MarketContext 时都必须注入无默认实现的 `CoverageAuthorityVerifier`，并重新验证同一
generation、source receipt/hash、taxonomy、sector denominator、`as_of`、verifier/proof 与验证时点。
缺 verifier、proof 不一致、拒绝或越过 PIT 时点时固定加入
`coverage_source_authority_unverified`，只能输出 `partial_market + degraded`。本地 fixture verifier
只用于合同测试，不能作为生产覆盖 authority。

只有以下条件全部成立才派生 `coverage_scope=full_market`：

- receipt `as_of` 与决策时点一致，membership 已生效且当时可知，`valid_until >= as_of`；
- mainboard、chinext、star、beijing 四个板块分母都存在，全部 observed=expected；
- sector count 数量等于 taxonomy 的明确 sector denominator，全部 observed=expected；
- 本次环境对象同时含创业板指数、科创板指数，并逐项物化 receipt 中全部且仅有的行业聚合 ID；
- receipt hash、source hash、正整数 generation、source receipt 和非空 lineage 均校验通过。
- 外部 verifier 在构造和消费两处均接受同一 authority binding，且验证 instant 位于 membership
  可知时点与决策 `as_of` 之间；ISO 字符串显示偏移不同不改变 instant 比较。

缺 receipt 或 PIT/内容/hash/schema 非法直接拒绝；过期、漏板块、漏行业、observed<expected、
observed>expected、taxonomy 分母不一致、创业板/科创板聚合缺失则只生成
`coverage_scope=partial_market`、`degraded=true`，并加入
`full_market_coverage_missing` 和精确 reason code（包括实际行业集合不等于 receipt 的
`taxonomy_sector_context_gap`）。降级快照仍可作为明确的局部环境证据，
但不得用于“全市场宽度完整”结论或提升仓位。

## 快照必需元数据

每层快照至少绑定：

```yaml
contract_id:
snapshot_id:
snapshot_sha256:
as_of:
source_snapshot_sha256:
catalog_version:
scope_policy_version:
account_permission_snapshot_id:
capital_authority_id:
authority_generation:
execution_lineage_id:
included_count:
excluded_count:
reason_codes:
```

环境聚合还需要上述内容寻址 CoverageReceipt 和 `context_only=true`。当前 `AccountTradableUniverseSnapshot` 的历史类型名只表示 `simulation_only` 主板scope policy，必须显式保存 `broker_permission_status=unverified`，不能冒充券商账户权限。最终订单前仍需要版本化账户权限、证券主数据、上市/退市、风险警示、停复牌、交易状态、真实持仓和可卖数量的 PIT 证据。

当前小资金候选快照把正整数 `authority_generation`、非空 `execution_lineage_id`、`single_name_max_pct`、`minimum_economic_order_cny`、`max_adv_participation_pct` 和 `lot_size_shares` 连同 cash、价格、费用/税/滑点版本及排除理由纳入不可变hash；非法参数 fail closed。generation/lineage 必须来自调用时已验证的 current capital authority，不得与历史 generation 1 或某个固定 lineage 常量比较。其 `max_buyable_shares` 只是在 `position_state_applied=false` 下的模拟cash+policy upper bound。只有叠加已验证 position/sellable/reservation/ledger authority 后，组合/风险层才能计算真实订单量。

当前本地快照候选尚不等于上述所有真实 SS/账户元数据都已接通；缺少时必须 degraded/reject，禁止用默认值补齐。

## 适合 50,000 CNY 的选择原则

小资金优势来自容量低、可等待、可拒绝、可使用小容量机会和可快速切换，不是“小市值=小资金适合”。个股只有在以下条件同时可计算时才能进入可行池：

- 一手金额不会突破单票 7,500 CNY 上限，且不使仓位粒度过粗；
- 扣除保守 commission、stamp duty、transfer fee、slippage 和未成交损失后，订单仍有最低经济意义；
- 流动性、停复牌、涨跌停、价格笼子、T+1 及隔夜尾部风险可在保守模型中重放；
- 同一产业、原材料、政策、事件、拥挤和模型论点的合并风险不超过当前人工审批 policy；
- 现金可以胜出，不因为需要保持交易频率而降低质量门槛。

这里的“人工审批 policy”不是调用方传入几个标签即可。当前本地候选要求`ThesisRiskRuntimeAuthority`对`industry / thesis / raw_material / policy_event / crowding / model_family`六维逐项给出有限非负cap，并用独立detached proof与完整exposure-set receipt覆盖候选、非零现仓和所有open/increase pending预约。同一symbol的candidate、position和pending group必须连续，day loop把每个决策group重新绑定权威receipt；pending卖出不重复计入。缺成员、过期、重复、篡改、改换group、runtime自签、嵌套proof冒充可晋级或跨决策把既有暴露清零均fail closed。当前fixture policy不可晋级，也不证明真实行业映射或实盘上限合理。

第一阶段只使用冻结 rank score 排序和经济性门禁，不接受自报 expected edge 作为仓位权威，也不把 rank score 说成概率。

按 100 股整手与 7,500 CNY 单票上限，一手预留金额在未计费用/缓冲前就要求股价低于约 75 CNY；实际阈值必须再扣 canonical 费用和保守预留，不能把 75 CNY 写成静态准入线。低价也不自动适合：若达到 2,000 CNY 最低经济订单需要过多手数、流动性不足或论点风险集中，仍应排除。行业标签和公司市值都不能替代这项逐证券计算。

## 零泄漏边界

非沪深主板个股在 Feature、Candidate、Forecast、TargetPosition、TradeIntent、ShadowBook、Order、Fill 和 Position 全链必须保持为零。创业板和科创板只允许指数与行业聚合影响市场环境、行业宽度或风险状态；coverage count 只证明聚合分母，不能转换成双创个股 identity。缺少完整 CoverageReceipt 或实际双创聚合时标记 degraded，禁止用主板子集冒充全市场宽度。

最终 OMS / 模拟执行前必须再次校验 scope。LLM、新闻、产业、资金或外部市场证据不能绕过该校验，也不能把 `context_only=true` 对象转换成个股订单。

零泄漏需要两类负例：一类证明 300/301/688/689、北交所、B 股、ETF/基金/指数和未知板块无法获得个股交易 identity；另一类证明创业板/科创板指数和行业汇总可以在环境层被消费，却无法穿透到个股 Feature 或 Order。

## 扩容规则

未来即使开通创业板、科创板或其他权限，也必须发布新的版本化 scope policy，重新完成数据契约、回测、影子运行、小资金可行性、风险和人工批准；权限变化不能自动扩大当前训练、候选、模拟或实盘范围。
