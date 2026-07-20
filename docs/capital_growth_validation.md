# Capital-growth 验证口径

> 本文定义 A股和 CNFutures 的样本、费用后结果、回撤、MG 消融与成熟度验收。它不构成收益承诺、投资建议或实盘授权。

## 1. 先验边界

- A股和 CNFutures 各自以独立 fresh-start 50,000 CNY simulated authority 运行；资金、PnL、DD、样本和成熟度不合并。
- Crypto 的 10,000 USDT 本地 fixture opening candidate 由其市场 lane 独立验证，尚不构成 current/runtime capital authority，也不纳入本文国内资本 KPI；即使币种相同也不得跨 market/account 聚合，All Markets 只汇总非货币计数与健康状态。
- 首 1–2 周只能证明工程/数据闭环和初步样本质量，不能证明长期正期望。
- 第 5、10 个 A股交易日是人工 review checkpoint，不是自动实盘日期。
- `promotion_evidence_ready`、短期盈利或胜率均不构成授权；自动 champion、自动风险扩张、自动 live transition 始终关闭。
- CNFutures 长期模拟，无实盘时间表。
- “月收益 20%”只能作为收益分布上尾的 stretch scenario，报告 `P(monthly_return>=20%)`、负月概率、尾部亏损与风险毁灭概率；不得作为第一阶段 PASS、最低交易频率或强迫交易条件。

本轮主板小资金架构仅是本地未提交候选；没有 TradingDatas fresh handoff、真实 V1 runtime、生产 scheduler 和真实 paper samples 时，只可评价契约/故障负例，不可评价策略正期望。A股个股样本仅限沪深主板普通 A 股；创业板、科创板指数和全市场行业聚合只作 `context_only` 环境证据。环境汇总缺失时行业宽度 degraded，不得用主板子集补分母。

### 1.1 第一阶段四层验收

| 层级 | 需要的证据 | 不能推断 |
|---|---|---|
| 契约层 | TradingDatas fixture/catalog/query、PIT gate、三层 Universe、50k plan binding、rank score、Opportunity/Ledger、forecast、三风格shadow router、RunBundle、ledger/OOS/LLM负例 | TradingDatas live、真实模拟成交、shadow预测有效性 |
| 本地闭环层 | 受控离线 replay、crash/restart、现金/持仓/冻结额守恒、原子 readback | 生产 scheduler 或稳定运维 |
| 20 交易日工程层 | 真实 TradingDatas V1 数据下 0 未来数据、0同 bar、0重复 order/fill、0权限泄漏、0旧链 fallback、0未解释账务差异 | 策略正期望或实盘准备 |
| 60–120 交易日科学层 | 冻结 OOS、独立 decision clusters/N_eff、多市场状态、完整成本/未成交、可发布的校准或排序证据 | 自动晋级、扩风险或 live |

第一阶段 Champion 是 `uncalibrated_deterministic_rank_score`。只验收排序一致性、分组结果、稳定性、与现金/简单可行基线的费用后增量和尾部风险。没有冻结 calibrator 时，概率、Brier、Log Loss 和 ECE 都不在Champion验收面中；分离的forecast shadow可以保存带detached proof的校准研究artifact，但不能把概率回写Champion或订单链。

`ValidationPlan` 本地合同已要求标签期限、最大特征回看、purge/embargo、事件簇隔离、decision-cluster 去重、注册试验数与总预算、PBO/Deflated Sharpe、OOS 重用上限和冻结 OOS authority receipt。A股另要求无默认calendar verifier，detached proof绑定dataset/receipt、完整交易会话与计划冻结时点；SampleJournal与A股label/sample ops必须显式接收该计划，CLI只通过`--validation-plan-path`加载外部预冻结的内容寻址artifact，不运行verifier或自签proof；`close/1d/3d/5d` target从同一会话authority派生且不得顺延。它只能证明本地输入绑定与错误配置会被拒绝；生产calendar authority、受信artifact registry、真实exit/总回报/公司行动真值、purged/nested walk-forward、PBO、DSR、多重试验和结果artifact尚未完成时，科学层仍是blocked。

### 1.2 自我进化验收边界

学习环可以自动做的事只有：生成候选实验、影子评估、漂移/质量检测，以及隔离、reduce-only、stop-new-risk 或 require-review 等负向动作。以下任意一项可自动发生即为失败：

- Challenger 取代 Champion；
- 放大单票/总敞口、改账户权限或扩大个股板块范围；
- 把 paper/shadow/audit-only Decision Ledger 事件伪装成 market-truth 标签；
- 在无外部 frozen label authority、冻结OOS registry、总回报/公司行动真值和精确 content/receipt/time binding 时发布 predictive eligibility；
- 用调用方自报`healthy`、短样本或旧metrics清除已经持久化的负向风险latch；
- 修改 `REAL_TRADING_ENABLED`、broker、邮件、GUI 或真实账户路由。

负向控制器必须显式使用`TrustedEvolutionClock`复核metrics freshness并把clock identity/read time写入结果；当前只有冻结、不可继承、`production_eligible=false`的fixture clock。当前metrics verifier也只对固定本地实现和内容binding做复核，不是第二套独立数值重算系统。因此“clock通过”和“detached receipt通过”都不能推断生产时间authority、指标正确或可自动解除latch；metrics超过14天、clock倒退或任一binding漂移只能保持/收紧风险。

### 1.3 LLM 增量价值怎样验收

DeepSeek等provider仅生成证据sidecar。在进入任何结构化研究输入前，要用冻结数据集比较“不用LLM”与“使用LLM证据”，至少评估：

- 实体、数值、事件、时点和影响路径抽取正确率；
- 固定Prompt版本、source span/hash、document hash、PIT时点、实体解析版本和EvidenceArtifact验证覆盖；
- 矛盾发现率、无依据断言率和非法 schema 率；
- 已知提示注入模式的阻断率、正常文本误报率、人工复核率，以及语义/编码变体的明确未覆盖率；
- 延迟、token/费用、敏感载荷拒绝和 outage 降级；
- 人工审核时间是否下降，以及给冻结基线模型带来的样本外增量是否足以覆盖复杂度。

LLM 输出不能直接作为 rank score、概率、仓位乘数、风险豁免或订单字段。动态Prompt、未验证artifact、未知引用、敏感payload或显式source-span提示注入模式必须在transport前阻断。typed source proof/provider receipt只证明对应offline或HTTPS transport的内容与操作元数据绑定，不证明provider输出正确、真实账户可用、生产verifier或收益增量。成功且完整验证的fixture/HTTPS evidence可进入CAS/hash-chain本地journal，但本地`.head`不是外部密封；同时替换或删除journal与head仍不能由本机自证。模式门也不是完整语义安全保证，所以当前不能宣称LLM已提高收益、研究质量或已解决prompt injection。

`ProviderRejectedAttemptReceipt`只记录真实HTTP provider envelope到达后被evidence schema/binding拒绝的脱敏审计事实，固定`evidence_journal_eligible=false`、`production_eligible=false`且全部authority为false。它不得进入SampleJournal、任何训练/评测样本、成熟度分母、模型晋级、LLM增量实验或自动演化输入；一次schema-rejected canary只能计入失败率和运维审计，不能作为“调用成功”样本。

## 2. V1 样本与决策账本验收

### 冻结 Champion observation/counterfactual

- 所有 data-qualified 主板候选由同一冻结 rank-score Champion 生成 prediction snapshot；组合、风险和执行门禁可以拒单，但不能抹掉 observation 或拒绝原因。
- 当前 V1订单链不运行多风格路由，不输出校准概率，也不接受 LLM/行业/opportunity/forecast/style shadow改写 rank score。三风格router虽已形成内容寻址本地shadow receipt，但只写反事实研究；paired MG on/off 也仅可作为绑定同一 base snapshot 的研究消融。
- 数据/PIT 不可靠时 prediction 只作审计，label eligibility 必须 rejected/unavailable；不能把坏数据混成有效 market-truth 标签。

### Decision Ledger 四态

- 每个候选最终只能形成 `PAPER_FILLED`、`PAPER_NOT_FILLED`、`REJECTED` 或 `OBSERVATION_ONLY`，并绑定 run、input bundle、capital authority/generation、execution lineage、prediction cluster、plan 和 drift constraint。
- Decision Ledger 是 audit-only 事实；paper、fixture、shadow 或人工拒绝结果不能因时间经过而自动成为 predictive label 或晋级证据。
- 同一股票同日最多一份 authority-bound 模拟订单；无交易必须给出数据、经济性、现金/T+1、风险、漂移、scope 或执行等具体 reason code。
- 每次risk评估和网络关闭的simulation副作用前都必须重读最新drift latch；任何运行中收紧都不得被缓存结果放宽。未来live broker仍需在真实外部副作用前给出同等authority证据。

### 多风格与 exploration 的状态

仓库旧四风格、exploration/exploitation 样本字段属于 time-boxed legacy/历史投影，不得恢复。当前新router仅含`industry_trend / event_surprise / cross_market_dislocation`三个shadow sleeve，按evidence group去重；支持与反对冲突时abstain，并固定无决策、资本、订单、自动晋级、自动扩风险或live authority。它可以进入单独shadow KPI，但不得混入V1当前订单或Champion绩效。未来若影响真实候选，必须发布新consumer合同和独立冻结验证，不能把旧路径或shadow receipt直接接回。

### Opportunity / forecast / router 科学晋级门

合同通过不等于发现能力、预测能力或多风格增量成立。后续只在冻结shadow样本中逐层消融：

- OpportunityRadar：报告真实可验证分母、`Capture@K`、`PreTriggerCapture`、`TimeToDetect`、precision/false-discovery、状态迁移稳定性、可成交比例和错失原因；只有scanned universe完整时才能声称全Universe覆盖；
- 多期限forecast：逐horizon、market regime、行业和流动性桶报告quantile loss、区间覆盖/宽度、Brier、Log Loss、ECE、base-rate skill、hazard censoring/competing-risk处理和成本后决策增量；禁止用同一OOS反复调参；
- 三风格router：比较Champion、Champion+单sleeve、Champion+去重router，报告evidence-group消融、abstain coverage/价值、费用后expectancy、tail loss、回撤、换手、风格与论点相关性以及冲突样本结果；
- 任一层只在独立decision clusters、冻结OOS和相同可成交/成本口径下比较。若收益主要来自少数日期、股票、行业或一个共同价格证据组，结论保持`insufficient_evidence`。

## 3. Execution-eligible 验收

### A股

- 真实 TradingDatas price/volume/source/timestamp，成交时段，普通A股与流动性，T+1、涨跌停、方向正确的整手/零股卖出规则、cash/positions、幂等全部通过。
- risk/order绑定同一`tradingagent.small_account_plan_receipt.v1`；无默认`AccountAuthorityVerifier`已逐项复核模拟capital generation、完整账户内容、position receipt/hash、cash/gross、mark、sellable数量和有效期，订单的symbol/side/quantity/reservation price/fee逐项相等。fixture proof不可晋级，也不证明真实账户。
- optimizer与day loop还必须共同证明六维论点风险：显式人工policy、逐成员detached proof和完整候选/持仓/open-or-increase-pending exposure set在决策时有效；每笔notional delta、同股票group连续性、pre/post/final exposure map与plan hash可独立复算。缺成员、重复成员、过期proof、运行时自签、替换policy后重签、pending漏记或跨决策清零都必须拒绝；超cap不得锁死经过验证的reduce/exit。fixture authority不可晋级，也不证明生产风险上限合理。
- plan必须绑定`cost_policy_id`，day loop按canonical佣金、过户费和卖出印花税独立复算；篡改费用后重新签名仍须拒绝。
- 买入只允许100股整数倍；卖出只允许100股整数倍、完整零股余额或全部退出，并受T+1可卖量约束。非法数量不得自动取整或改写。
- 单票累计“当前持仓市值 + pending reservations + 新订单”不超过 7,500 CNY；组合 gross 不超过 45,000 CNY；容量最多 8 且可支持至少 7 个不同股票。
- actual fill quantity/price/time、commission/stamp duty/slippage 和 receipt/local-trade fingerprints 完整。
- 买入 `fill_commit` 或卖出 `ashare_sell_commit` 成功/幂等成功；outbox pending、CAS/lineage 冲突或请求值兜底只能进入 chain validation。
- 决策、fill/terminal和reconcile时钟单调；任何fill早于decision或reconcile早于terminal均不形成execution-eligible样本。

### CNFutures

- multiplier/tick、最小一手、保证金/手续费、价格限制、滑点、夜盘跳空、会话、换月、持仓和止损损失预算均有可追溯来源。
- 保证金使用总额不超过 25,000 CNY；该上限不能替代单笔止损预算。
- actual open fill 通过 `fill_commit`；actual close/reduce 通过 `position_close_commit`；margin/fee/PnL、position fingerprint 与 ledger head CAS 完整。
- 任一条件不适配时 `counterfactual_only=true`、quantity=0，方向预测与标签继续。

### Crash-replay

- immutable fill 只生成一个 durable outbox action；相同 identity/payload 可幂等重放，冲突 payload fail closed。
- partial 只消费实际数量，terminal 原子释放未成交预约。
- 崩溃后 replay 不重复成交、费用、PnL 或释放；pending action 对新增风险保持保守占用。
- 每日 MTM reconcile 的 exact reservation manifest、未结 commit IDs、持仓/保证金/冻结额和 execution lineage 必须一致。

## 4. 成本与标签

规范 horizon：`m30/m60/close/1d/3d/5d`。

- `as_of` 阻止未来泄漏；日线不伪造 m30/m60，晚到价格不回填更早 horizon。
- 反事实使用版本化保守成本模型；输出 cost version、fee/slippage assumptions 和 net return。
- 真实成交绩效只使用 actual commission、stamp duty、slippage 和 actual fills；默认 0 成本或估算请求价格不进入绩效。
- 前向标签按 ready/pending/missing/rejected 分类；missing/rejected 原因分布必须可见。
- A股`close/1d/3d/5d` targets必须由同一verified frozen calendar proof派生，调用方不一致立即fail closed，缺目标会话日线不得顺延。A股label/sample ops缺显式计划时必须在读取行情前阻断；CLI加载artifact只校验合同/内容绑定，不重新证明上游authority。当前仅有本地/fixture verifier且无受信artifact registry，target authority也不等于真实exit price、总回报或公司行动真值，因此不能单独作为predictive发布证明。
- 5 分钟重复 cluster 只给一个有效 KPI 权重；原始事件仍保持 append-only。
- 标签格不等于独立样本：验收同时展示 `ready_label_cell_count/raw_N/unique decision clusters/independent trading days/N_eff`，成熟度只使用预先指定主 horizon 的独立 decision cluster。
- PIT 必须重算 `event_time/available_at/ingested_at/retrieved_as_of` 的完整性与顺序；字段存在或布尔自述不算通过。

sample ops P0 还必须证明以下 frozen-input 与性能不变量：

- 14:19/14:21 cutoff 区分 prediction time 与晚到 receipt/availability；顶层与 nested PIT receipt/availability 均取最晚，任一存在但非法/无时区 fail closed；
- 运行中追加 4,001 条时，本轮 H0 不变化，下一轮可见；frozen head 后未知 append 阻断，task-owned delta 仅包含本任务 label；
- 同日 1,999 terminal + 1 pending 时只选择 1 个 snapshot ID，不能重跑整日 2,000 个 predictions；
- 2,000 snapshots、250 symbol-date、8 variants 的行情调用有确定上界，logical/physical/cache 指标可核对；
- provider timeout/degraded 保留 observation 和 retryable pending，不生成 terminal；
- 每 100–250 labels 一批，批前/批后 crash replay 均不重复 event，前缀冲突 fail closed；
- labels、KPI、decision、maturity 来自同一 H1 与 `projection_input_sha256`；最后 label batch 与 pointer publish 之间的未知 append 被最终 CAS 阻断；原子发布中断后 current 仍是旧完整 generation；pointer 的 manifest SHA 能检测任意 manifest 字段篡改；reader 重算 generation ID，复制三投影并重签 manifest/pointer 到伪造 ID 仍必须拒绝；
- generation 存在但 current 缺失/非法时健康检查与前端 fail closed；仅明确无 generation 的 legacy 健康回退标 degraded，并强制 non-mature stage、maturity evidence untrusted、promotion false；安全字段缺失不能被当作 false；
- 对固定 immutable evidence，新旧 label/KPI/maturity 逐字段一致；所有双 50k、authority/lineage、live marker、`REAL_TRADING_ENABLED=true` 与自动晋级门禁仍 fail closed。reference selection 还必须证明两种 provider 输入顺序下 invalid/future sibling 不能覆盖合法 row，无合法 row 全链 pending/not-selected；projection publication 必须证明 final validation 后的 generation in-place/rename/hardlink 与同字节、同 mode、不同 inode 替换，以及 mirror/log 各自的 rename/symlink/hardlink，都不能改变旧 current bytes。

阶段报告至少包含 wall/CPU、Journal events/bytes/parse、锁等待/持锁、append batches/fsync、pending/selected/terminal、HTTP logical/physical/cache/timeout/retry/latency、as-of drift 和 projection generation。合成本地 benchmark 只证明算法调用上界与回归，不代表生产延迟、provider 容量或策略收益。

## 5. KPI 必须分层

V1 当前至少按 market、决策四态、主 horizon、数据状态和 reason code 显示：

- candidates、predictions、observation/counterfactual；
- paper filled/not-filled、rejected、observation-only；
- completed round trips、exit/stops；
- risk rejects、chain-validation samples；
- 每个 horizon 的状态；
- win rate、average PnL/win/loss、expectancy；
- gross PnL、fees/slippage、post-cost PnL、账户逐日 MTM max drawdown；
- rejection/missing-evidence distributions；
- authority/generation/execution lineage 和 excluded legacy count。

历史 exploration/exploitation 或风格字段若仍存在，必须单列为 legacy/excluded，不能混入 V1 结果。风格 shadow 与行业 shadow 都不产生资本；A股与 CNFutures 货币指标不能相加。

completed round trip 缺 gross 或 net 数值时计入 invalid evidence，不得进入胜率/expectancy/PnL。交易 PnL 序列回撤只作为辅助字段，不能替代账户逐日 MTM equity 曲线。

## 6. 资金利用率验收

当前 `minimum_economic_order_cny=2,000` 与 `no_trade_band_cny=1,000` 是 Phase 1
首版保守、版本化的工程假设，不是统计最优值。真实模拟成交积累后，必须按账户实际最低佣金、
印花税/过户费、滑点、未成交损失、信号半衰期和整数股误差做冻结OOS敏感性分析；只能由新 policy
版本和人工复核调整，不能为提高交易频率或回测收益在线调参。

A股资金计划每天保存：

- deployed、committed、planned stock exposure；
- `deployed_utilization_rate`、`committed_utilization_rate`、`planned_stock_utilization_rate`；
- dynamic operating cash 及组成；
- undeployed/planned undeployed capital；
- position capacity/remaining slots；
- data-qualified/execution-eligible candidate counts；
- `undeployed_reasons` 的 code、amount 和 details。

“资金未闲置”的含义是没有人为固定保留池，合格机会可使用全部账户；弱市、无正期望、整手/成本不适配或硬门禁未过时可以持有现金。不得为了提高利用率强行买入。

现金管理建议单独记为 `cash_management_yield`，`auto_order=false`，不并入股票 alpha 或伪造部署率。

## 7. MG paired ablation

有效 MG 增益比较要求：

- 同一 `base_snapshot_sha256`、prediction timestamp、candidate universe 与 data-quality；
- `mg_off` 不含 MG feature；
- 同一 horizon、label source、cost model 与样本去重规则；
- 比较 calibration、net-after-cost expectancy、drawdown 和 regime robustness，而非只看短期胜率。

Calibration 必须输出独立 cluster 的 Brier、log loss、base-rate Brier/skill 与 reliability ECE；任意 `calibration_evidence_sufficient=true` 字段不得直接通过门禁。未校准 score 保持 `rank_score` 语义。

缺少 paired samples 或样本外证据时，结论只能是“未验证”，不能据此扩风险。

## 8. A股 day-5/day-10 review

| 交易日阶段 | maturity stage | 要求 |
|---|---|---|
| 1–4 | collecting | 每日 prediction、标签状态、具体 no-trade reason、execution chain |
| 5 | day-5 review due | 人工复核数据/链路/成本/风控/故障；继续 sim |
| 6–9 | continued simulation | 修复缺口并扩大市场状态覆盖；不自动晋级 |
| 10 | day-10 review due | 第二次人工复核；仍需 Nicholas 单独授权 |
| 11+ | post-day-10 evidence | 持续积累样本外证据；没有自动 live |

当前 evidence-readiness 实现至少检查：当前 authority/lineage、20 个 execution-eligible samples、预先指定主 horizon 的 20 个 unique decision clusters、至少 5 个独立交易日、`N_eff >= 10`、10 个 completed round trips、chain consistency ≥0.85、data integrity ≥0.90、完整 actual-cost/PIT/fill-revalidation/dedup/calibration evidence、至少一个费用后正 expectancy 风格，以及账户逐日 MTM 最大回撤不超过 5%。style×horizon label cells 只展示，不计作独立 N。

这些数值只是旧成熟度投影与当前工程验收的最低门槛：样本量仍很小，不能据此声称统计显著或自动实盘。任何缺失项显示 blocker，但不阻断安全 observation；是否重新引入 exploration 必须由未来版本另行批准。

潜力股捕捉率验收同时列出 full eligible universe、实际 scanned universe 与 top-K。若 full universe 不完整，报告只能声称 scanned-universe recall；benchmark 缺失则 alpha/excess return 保持 null/status unavailable，禁止用 0 代替。

## 9. CNFutures 长期成熟度

期货 maturity 与 A股日期无关。当前最低工程分层检查包括：至少 5 个有效样本、3 个完整回合、2 个独立品种、2 个波动 regime、夜盘/换月/极端风险覆盖、费用后正结果、最大回撤不超过 5% 和稳定性分数至少 0.55。

这些门槛只用于成熟度分类，不设置实盘日期，也不自动扩保证金或风险。持续补充不同品种、波动、会话、夜盘、换月、费用/滑点和极端行情证据。

## 10. 实盘门禁（与模拟样本分离）

模拟 observation/paper 的作用是验证数据、决策和执行链；实盘晋级门禁负责阻止真钱风险，两者不能共用“运行过/有收益”这一类宽松阈值。

A股只有同时满足以下条件并经 Nicholas 明确确认，才可另行设计人工试运行：

- signal → order → receipt → position → capital → journal 全链一致；
- actual costs、整手、滑点和成交证据稳定；
- 多市场状态/故障/降级覆盖充分；
- 费用后 expectancy、calibration 和 drawdown 证据可接受；
- 回滚与人工操作流程经过独立验收。

试运行仍是完整 50,000 CNY 账户，但初始订单敞口控制在 20%–30%，不得自动扩仓。邮件/同花顺路由未实现；设计未获审阅前不得编码或发送。CNFutures 不进入此流程。

## 11. 每周报告结论词汇

- `closed_loop_engineering_passed`：只表示工程闭环通过。
- `evidence_collection_in_progress`：有样本但成熟度不足。
- `promotion_evidence_ready`：最低证据检查通过，仍非授权。
- `not_authorized`：没有 Nicholas 明确 live/pilot 授权。
- `insufficient_evidence`：说明具体缺口，不能简写为“策略失败”或“样本不足所以零 observation”。

禁止使用“稳定盈利”“已验证高胜率”“可自动实盘”或把模拟收益外推为未来收益。

运行命令见 [operations.md](operations.md)，字段见 [data_contract.md](data_contract.md)。
