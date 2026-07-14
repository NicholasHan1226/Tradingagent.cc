# Capital-growth 验证口径

> 本文定义 A股和 CNFutures 的样本、费用后结果、回撤、MG 消融与成熟度验收。它不构成收益承诺、投资建议或实盘授权。

## 1. 先验边界

- A股和 CNFutures 各自以独立 fresh-start 50,000 CNY simulated authority 运行；资金、PnL、DD、样本和成熟度不合并。
- 首 1–2 周只能证明工程/数据闭环和初步样本质量，不能证明长期正期望。
- 第 5、10 个 A股交易日是人工 review checkpoint，不是自动实盘日期。
- `promotion_evidence_ready`、短期盈利或胜率均不构成授权；自动 champion、自动风险扩张、自动 live transition 始终关闭。
- CNFutures 长期模拟，无实盘时间表。

## 2. 样本通道验收

### Observation/counterfactual

- 所有 data-qualified 候选生成 prediction snapshot；成熟策略阈值和执行门禁不阻断。
- 每个 A股候选包含四类正交风格，并基于同一 immutable snapshot 生成 paired MG on/off。
- 未成交风格也生成 forward labels；避免只学习成交样本。
- 数据不可靠时仍保存 prediction，但 label eligibility 明确 rejected；不能把坏数据混成有效标签。

### Exploration

- 只在硬门禁合格集合内运行 top-K stratified random/epsilon-greedy。
- 保存 policy version、seed、pool、selection probability/propensity 和选择/未选择原因。
- A股每日最多新增一个探索头寸；累计探索敞口不超过 7,500 CNY；探索日亏不超过 225 CNY。
- 只可降低 raw-score/min-edge/research-completeness 等策略门槛；所有数据、执行、资金和风险硬门禁不变。
- 无 exploration 时，理由只能是无 data-qualified candidate 或具体安全门禁；“样本不足”不能单独解释零交易。

### Exploitation

- 使用成熟策略门槛和组合预算；与 exploration 分开统计。
- 同一股票同日只有一份真实规格模拟订单；多风格只作 attribution。
- 新风格只进 shadow/exploration，不能因短样本表现进入自动 champion。

## 3. Execution-eligible 验收

### A股

- 真实 SharedSignals price/volume/source/timestamp，成交时段，普通 A股与流动性，T+1、涨跌停、100 股整手、cash/positions、幂等全部通过。
- 单票累计“当前持仓市值 + pending reservations + 新订单”不超过 7,500 CNY；组合 gross 不超过 45,000 CNY；容量最多 8 且可支持至少 7 个不同股票。
- actual fill quantity/price/time、commission/stamp duty/slippage 和 receipt/local-trade fingerprints 完整。
- 买入 `fill_commit` 或卖出 `ashare_sell_commit` 成功/幂等成功；outbox pending、CAS/lineage 冲突或请求值兜底只能进入 chain validation。

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

按 style、market 和 sample intent 显示：

- candidates、predictions、observation/counterfactual；
- exploration fills、exploitation fills；
- completed round trips、exit/stops；
- risk rejects、chain-validation samples；
- 每个 horizon 的状态；
- win rate、average PnL/win/loss、expectancy；
- gross PnL、fees/slippage、post-cost PnL、账户逐日 MTM max drawdown；
- rejection/missing-evidence distributions；
- authority/generation/execution lineage 和 excluded legacy count。

Exploration 与 exploitation 收益不能混算。风格 shadow 不产生资本；A股与 CNFutures 货币指标不能相加。

completed round trip 缺 gross 或 net 数值时计入 invalid evidence，不得进入胜率/expectancy/PnL。交易 PnL 序列回撤只作为辅助字段，不能替代账户逐日 MTM equity 曲线。

## 6. 资金利用率验收

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

这些数值只是当前 evidence-readiness 最低工程门槛：样本量仍很小，不能据此声称统计显著或自动实盘。任何缺失项显示 blocker，不阻断 observation/exploration 的安全采样。

潜力股捕捉率验收同时列出 full eligible universe、实际 scanned universe 与 top-K。若 full universe 不完整，报告只能声称 scanned-universe recall；benchmark 缺失则 alpha/excess return 保持 null/status unavailable，禁止用 0 代替。

## 9. CNFutures 长期成熟度

期货 maturity 与 A股日期无关。当前最低工程分层检查包括：至少 5 个有效样本、3 个完整回合、2 个独立品种、2 个波动 regime、夜盘/换月/极端风险覆盖、费用后正结果、最大回撤不超过 5% 和稳定性分数至少 0.55。

这些门槛只用于成熟度分类，不设置实盘日期，也不自动扩保证金或风险。持续补充不同品种、波动、会话、夜盘、换月、费用/滑点和极端行情证据。

## 10. 实盘门禁（与模拟探索分离）

模拟 exploration 的作用是避免长期零样本；实盘晋级门禁负责阻止真钱风险，两者不能共用一套过严阈值。

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
