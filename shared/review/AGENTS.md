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
- 同一决策时点的 style×MG×horizon 共用 `decision_cluster_id`；成熟度使用预先指定主 horizon 的 unique clusters、独立交易日与 N_eff，不使用 label-cell 总数。
- PIT 重新校验 event/available/ingested/retrieved 时间链；calibration 真实计算 Brier/log loss/base-rate skill/reliability，布尔字段不能自证。

## KPI 与成熟度

- 按风格和 sample intent 展示 candidate/prediction/fill/round-trip/exit/reject/chain counts、各 horizon 状态、胜率、平均盈亏、expectancy、gross/cost/post-cost PnL、最大回撤和拒绝分布。
- 组合资本、权益、PnL 和回撤只来自对应市场 authority；不同市场或风格 shadow 不聚合。
- 正式最大回撤只来自账户逐日 MTM equity；trade-PnL 序列回撤仅作辅助。benchmark unavailable 保持 null/status，不回落为 0。
- A股成熟度显示模拟交易日、day-5/day-10 review due、有效样本、科学证据、阻塞原因和人工授权状态。
- CNFutures 成熟度独立显示有效会话/样本、品种与波动覆盖、夜盘/换月/极端风险、完整回合、费用后结果和稳定性。
- `promotion_evidence_ready` 只表示证据检查结果；`automatic_promotion_enabled=false`、`automatic_risk_expansion_enabled=false`、`live_transition_authorized=false` 始终保持。
- 短样本正收益不等于可重复盈利；机械闭环通过也不等于策略成熟。

统一运行入口是 `python3 -m shared.runtime_test.ashare_sample_ops`；它只生成标签和投影，不创建订单、账户、邮件或 live transition。字段见 [../../docs/data_contract.md](../../docs/data_contract.md)，验收见 [../../docs/capital_growth_validation.md](../../docs/capital_growth_validation.md)。
