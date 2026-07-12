# TradingAgent / CNFutures

CNFutures 是国内期货的长期模拟研究与样本闭环。当前目标是持续保存方向判断、风险拒绝、反事实标签和真实规格模拟成交证据；它不承担 A 股资金加速，也没有实盘日期。

长期规则见 [AGENTS.md](AGENTS.md)。跨市场架构、字段、验收和运行方式分别见：

- [../docs/architecture.md](../docs/architecture.md)
- [../docs/data_contract.md](../docs/data_contract.md)
- [../docs/capital_growth_validation.md](../docs/capital_growth_validation.md)
- [../docs/operations.md](../docs/operations.md)

## 当前边界

- 唯一资本 authority：`cn-futures-capital-v1`，generation 1，fresh-start simulated 50,000 CNY。
- 最大保证金使用：25,000 CNY（50%）。保证金容量与止损损失预算分别校验。
- A 股与 CNFutures 的现金、保证金、PnL、回撤、execution lineage、样本和成熟度完全分离。
- `REAL_TRADING_ENABLED=false`。不得连接真实 broker、发送真实委托或自动切换 live。
- 5% 回撤只收紧风险预算，7% 回撤暂停；日亏和连续亏损门禁按期货账户独立触发。
- 静态合约规格只用于模拟 bootstrap，不能被描述为交易所级撮合、保证金或强平精度。

## 运行闭环

```text
SharedSignals Futures bars/spec evidence
  -> strategy prediction / hold / risk reject
  -> one-lot affordability and hard risk gates
  -> simulated execution evidence + market capital commit
  -> append-only order lifecycle events + startup directory reconcile
  -> append-only CNFutures review journal
  -> m30/m60/close/1d/3d/5d forward-label updates
  -> Sample KPI + independent maturity projection
  -> observation report / read-only dashboard
```

每个有效会话至少保留 prediction、hold、risk reject、counterfactual 或 simulated fill 之一。样本不足不能阻断 observation；但数据、时段、最小一手、真实规格、费用、滑点、夜盘、换月、资金、回撤和 execution lineage 门禁不能为了采样而放宽。

方向预测与可执行性必须分开：不适配 50,000 CNY 账户的一手合约仍可形成 `counterfactual_only=true` 的方向样本和后续标签，但不能伪造成可执行成交。

## 当前事实源

| 事实 | 路径 |
|---|---|
| 资本事件与 reconcile | `shared/logs/capital/cn_futures/` |
| 持仓、回执与 durable outbox | `signals/` |
| append-only 订单事件 | `signals/order_events/cn_futures_order_events.jsonl` |
| 订单事件投影 | `signals/order_events/cn_futures_order_projection.json` |
| append-only 会话/样本 journal | `shared/review/data/cn_futures_sim_reviews.jsonl` |
| 当前成熟度/KPI 投影 | `shared/review/cn_futures/market_maturity_latest.json` |
| 只读观察报告 | `shared/review/cn_futures/observation_report.json` |

`market_maturity_latest.json` 只是可重建投影。只有 canonical `projection_sha256`、report type、evidence source、`cn-futures-capital-v1` / generation 1、非空 execution lineage、50,000/25,000 资金口径、来源 SHA、sim-only 标记和 manual-review-only 策略全部一致时，才可用于当前成熟度展示；任一字段被改写后，观察报告和健康检查都会 fail closed。

## 演化规则

append-only review journal、前向标签、actual-cost execution evidence、Sample KPI 和成熟度是当前唯一演化证据。旧自动调权、自动生成变体、自动晋级和自动风险扩张均已退役。

订单状态目录只是兼容投影：启动必须用 checksum-chain order journal 重建并核对。当前本地 IOC 模拟允许 `partial` 作为明确终态；schema 同时表达未来异步订单的非终态 partial/`REDUCING`，但 broker、邮件和同花顺流程仍是 design-only，未实现。CN review 没有同频净收益序列时不计算 Sharpe/DSR；`net_pnl_to_drawdown_plus_fee_ratio` 只作诊断，永不作为晋级证据。

- runtime 不读取旧自动覆盖结果；
- 当前成熟度只做 assessment，不写策略、不调仓、不生成订单；
- `promotion_evidence_ready` 只表示证据检查结果，不构成任何实盘或扩风险授权；
- CNFutures 长期保持 `manual_review_only_no_futures_live_date`；
- 旧自动演化 Python 入口、wrapper 与 schedule 已物理删除，不保留可被 stale caller 重新调用的 tombstone。

## 本地只读/模拟检查

以下命令不会安装 cron，也不会连接真实交易：

```bash
REAL_TRADING_ENABLED=false python -m shared.runtime_test.cn_futures_live_check --pretty

REAL_TRADING_ENABLED=false python -m shared.runtime_test.cn_futures_sample_ops \
  --trade-date 20260713 \
  --as-of 2026-07-13T15:10:00+08:00 \
  --pretty

REAL_TRADING_ENABLED=false python -m CNFutures.observation_report --pretty
```

服务器模板入口为：

- `shared/wrappers/job_cn_futures_sim.sh`
- `shared/wrappers/job_cn_futures_sample_ops.sh`
- `shared/wrappers/job_cn_futures_observation_report.sh`
- `shared/wrappers/job_market_capital_reconcile.sh cn_futures`

仓库中的 wrapper 与 crontab 只是模板；文件存在不等于生产 cron 已安装或 runtime 已切换。生产初始化、reconcile、cron apply、部署和发布均需单独 preflight 与 Nicholas 授权。

## 验收重点

- 当前交易会话有 prediction/hold/reject/fill 事实，不能静默零样本。
- 反事实与可执行成交分层，费用/滑点后的结果不与方向样本混算。
- execution-eligible fill 有实际 fill、手续费、滑点、合约规格、资本 commit 和 PIT/source SHA。
- partial fill、平仓、夜盘、换月、极端风险和 crash replay 可复盘。
- 成熟度按品种、波动、会话、夜盘、换月、极端风险、费用后结果、回撤和稳定性独立展示。
- 观察报告与健康检查忽略任何旧自动演化覆盖文件，且始终显示自动晋级/风险扩张/live transition 为关闭。

当前本地与生产状态只看 [../STATUS.md](../STATUS.md)，不能把本地测试通过表述为已部署、已安装 cron、已积累真实市场样本或已验证盈利。
