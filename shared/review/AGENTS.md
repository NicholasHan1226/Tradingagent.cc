# TradingAgent / shared/review

> 阅读顺序：[../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件。

## Authority

- A股唯一演化事实源：append-only `shared/review/ashare/sample_journal.jsonl`。
- `sample_kpi_latest.json`、`evolution_decision_latest.json`、`market_maturity_latest.json` 及对应 log 是可重建投影，不可反写或覆盖 journal 事实。
- 只有当前 `capital_authority_id + authority_generation + execution_lineage_id` 的记录进入 KPI；历史/legacy 记录保留但从当前统计排除。
- 旧 portfolio/weekly/legacy review 不能给出自动 lifecycle、champion 或风险晋级。

## 样本分层

必须分别统计：

1. `observation_counterfactual`
2. `exploration_fill`
3. `exploitation_fill`
4. `completed_round_trip`
5. `exit_stop`
6. `risk_reject`
7. `chain_validation`

chain validation、盘外、缺来源/lineage、弱成交证据或 capital commit pending 样本只保留审计，不进入胜率、expectancy、费用后 PnL 或晋级证据。

## 预测、消融与标签

- 所有数据合格候选保存 prediction；成熟/执行门禁不阻断 observation。
- paired MG 消融必须共享 immutable base snapshot、prediction time、data quality 和 label/cost口径；`mg_off` 不读取 MG 特征。
- raw heuristic score 与 uncalibrated prior 必须如实命名，不能包装成校准概率。
- Exploration 保存 policy version、seed、top-K、selection method、selection probability/propensity 和未选择原因。
- horizon 固定为 `m30/m60/close/1d/3d/5d`；`next_day` 仅是 `1d` 兼容别名。
- `as_of` 防止未来泄漏；日线不能伪造 m30/m60。反事实标签使用版本化保守成本，真实 round trip 使用 actual fee/slippage。
- 5 分钟重复样本按 cluster 去重；未成交风格同样生成标签，避免选择偏差。
- 同一决策时点的 style×MG×horizon 共用 `decision_cluster_id`；成熟度使用预先指定主 horizon 的去重unique clusters、不同交易日覆盖与propensity权重Kish N_eff，不使用label-cell总数，也不把这些计数解释为收益序列已独立。
- PIT 重新校验 event/available/ingested/retrieved 时间链；calibration 真实计算 Brier/log loss/base-rate skill/reliability，布尔字段不能自证。

## KPI 与成熟度

- 按风格和 sample intent 展示 candidate/prediction/fill/round-trip/exit/reject/chain counts、各 horizon 状态、胜率、平均盈亏、expectancy、gross/cost/post-cost PnL、最大回撤和拒绝分布。
- 组合资本、权益、PnL 和回撤只来自对应市场 authority；不同市场或风格 shadow 不聚合。
- 正式最大回撤只来自账户逐日 MTM equity；trade-PnL 序列回撤仅作辅助。benchmark unavailable 保持 null/status，不回落为 0。
- A股成熟度显示模拟交易日、day-5/day-10 review due、有效样本、科学证据、阻塞原因和人工授权状态。
- CNFutures 成熟度独立显示有效会话/样本、品种与波动覆盖、夜盘/换月/极端风险、完整回合、费用后结果和稳定性。
- `promotion_evidence_ready` 只表示证据检查结果；`automatic_promotion_enabled=false`、`automatic_risk_expansion_enabled=false`、`live_transition_authorized=false` 始终保持。
- 短样本正收益不等于可重复盈利；机械闭环通过也不等于策略成熟。

## 离线科学投影

- `shared/runtime_test/ashare_offline_science.py`只从显式`SampleJournal.read_frozen(as_of=...)`视图读取，构建与验收只能消费精确类型的同一`FrozenJournalView`，从完整source events重建cutoff分区、excluded/max evidence与included head，并用进程内HMAC seal绑定原始source digest/byte count和内部索引。该seal不是外部签名或durable authority。调用方必须独立提供预期cutoff和authority scope；报告内自报值不能成为自身验收标准。向仓外内容寻址目录发布outcome、counterfactual、费用后metrics、calibration/MG ablation和run receipt；不得追加或改写Journal。
- unique decision cluster是去重计数单位，不自证统计独立；必须分别展示观察交易日数、propensity权重Kish有效样本量与按最长主horizon移动观察交易日块估计的依赖修正样本量，不得把style×horizon label cells或字段名中的`independent`当作扩大N的依据。少于两个完整依赖块时置信区间保持unavailable。
- Outcome及所有下游科学报告必须携带精确source events与预测前冻结的ValidationPlan重建复核；只重算报告自哈希、跨authority label update、缺日历目标、缺真实exit/PIT/成本证据或label算术不一致均fail closed。
- counterfactual book是描述性研究投影，不是capital、position、order或PnL账本；MG消融必须共享PIT、成本和pair identity，缺配对保持unavailable。
- 小账户敏感性只允许预注册的`max_positions / minimum_economic_order_cny / no_trade_band_cny / cost_stress_multiplier`固定网格，并固定50,000 CNY、15%单票、90% gross和100股整手；排除最低经济订单小于无交易区的无效组合后必须发布全部96格，缺格/重复/替换均阻断，禁止从结果挑选winner或回写policy。
- 所有离线科学产物均无promotion、risk expansion或live transition authority；通过本地报告不能替代冻结OOS、外部label truth、生产metrics authority或人工复核。

统一运行入口是 `python3 -m shared.runtime_test.ashare_sample_ops`；它只生成标签和投影，不创建订单、账户、邮件或 live transition。字段见 [../../docs/data_contract.md](../../docs/data_contract.md)，验收见 [../../docs/capital_growth_validation.md](../../docs/capital_growth_validation.md)。

手工离线科学入口另为`python3 -m shared.runtime_test.ashare_offline_science`；它不是sample-ops替代品，不注册scheduler，也不能写默认Journal根。
