# TradingAgent / Ashare

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 定位与账户

- 本模块负责 A股候选、多风格预测、组合资金计划、T+1、server-local 模拟执行和样本归因；不采集行情，也不连接真实券商。
- 唯一执行账户是 fresh-start `ashare-capital-v1` 的 50,000 CNY simulated 账户；generation 1 只是初始化基线，每轮必须从 current snapshot 读取、验证并传播正整数 generation。
- 资本 ledger 位于 `shared/logs/capital/ashare/`，执行事实根必须由 current snapshot 的受验证 `execution_lineage_id` 动态派生到 `shared/logs/execution_lineages/<execution_lineage_id>/`；固定日期 lineage 与旧 `shared/logs/local_sim/` 均冻结，adapter 资金只可诊断。
- 股票总敞口上限 45,000 CNY，单票累计“持仓市值 + pending reservations + 新订单”上限 7,500 CNY，100 股整手；容量 8 并至少支持 7 个不同股票。
- 不设固定保护现金、不强制满仓。费用、滑点、冻结额与 pending order 构成动态运营现金；计划必须写利用率和未部署原因。
- 历史共享资金池、旧持仓/PnL、旧多账本只读冻结，不导入或计入当前统计。

## 样本顺序

1. 对所有数据合格的主板候选生成 observation/counterfactual；创业板、科创板个股禁止进入分析与订单，相关指数和行业聚合只作环境参考。
2. 以同一 immutable base snapshot 生成 paired `mg_on` / `mg_off`，后者不得含 MG 特征。
3. 唯一冻结 Champion 生成未经校准的 deterministic rank；该分数只排序，新仓由与 rank 无关的固定 probe sizing 进入 50k optimizer。
4. OpportunityRadar、Ledger、多期限 forecast 与 `industry_trend/event_surprise/cross_market_dislocation` 三风格只写 shadow/反事实证据，不进入 Champion、资金或执行链。
5. 单一组合决策器处理资金、论点风险、T+1、整数股、费用与幂等，同一股票同日最多一份 authority-bound 模拟订单。
6. 保存 observation/fill/reject/round trip/chain validation，并生成 `m30/m60/close/1d/3d/5d` 标签。

旧四风格、exploration/exploitation 与对应固定额度属于 time-boxed legacy，不是当前 V1 样本或执行入口。新三风格只作 shadow sleeve；现金是正式动作，不得用轻微阈值变化制造伪独立风格。

## 旧 Exploration 退役边界

- 每日最多新增 1 个探索头寸；探索累计敞口上限 7,500 CNY；探索日亏上限 225 CNY。
- 只可下调候选分数、最小 edge 或研究完整度等策略门槛。
- 数据来源/新鲜度、普通 A股与流动性、真实价格/成交证据、交易时段、T+1、涨跌停、100 股整手、现金/持仓、幂等、累计敞口、日亏、连续亏损、回撤和实盘隔离永不放宽。
- 这些数值只用于旧记录解释和退役回归，不得恢复旧 exploration writer。当前“样本不足”不能阻断安全 observation；无模拟成交必须记录无合格候选或具体硬门禁。

## 执行与资本一致性

- 当前 V1 仓库合同由 `compose_capital_backed_paper_runtime` 组合 canonical simulated account、capital-backed risk、模拟执行、outbox commit 与 reconcile。该 composer 保持 network-closed，且只接受 `FrozenFixtureHTTPTransport`；不得为生产会话放宽。一个自然 A 股交易日的资本记账由同仓 sibling `Ashare.capital_backed_paper_runner` 完成：它复用 `MarketCapitalLedger` / `CapitalBackedSimulationExecutionStagePort` / `ChampionSelectionRegistry.load_current`，消费已工作的 TradingDatas `catalog/query`，并把每个候选持久化为 `paper_filled` / `paper_not_filled` / `rejected` / `observation_only`。空 Champion registry 必须继续对 order-identity 名返回 `champion_current_unavailable`，不得把缺失 current 当成买入。科技+医药 paper 的第一份 simulation-only Champion 只能由 `Ashare.paper_champion_bootstrap` 通过 `record_selection`（AUTOMATION）写入显式 `registry_root`；禁止手写 `current.json`、伪造 KPI/round trip，或启用 promotion/live timer。simulation-only current 加上空 50k simulated book 不得仅因 oneshot 把 `drift_ok` 写死为 false 就被 `drift_constraint_blocks_new_risk` 整批拒绝；`real_trading` / `live_transition_authorized` / `automatic_risk_expansion` 的 live-risk drift 继续 fail-closed。默认成交路径只能是 `CapitalBackedSimulationExecutionStagePort` 加 ledger commit；日线 close/touch 不是 quote clock，也不得发明 fill。现金时段 oneshot 绑定当日 `cn.market.trade_calendar` 与上一完整日 `cn.equity.daily`（calendar `pretrade_date`）；日线查询必须带候选 `ts_code` 过滤（可分片），不得无过滤拉整分区，也不得把 413/budget_exceeded 吞成 `missing_prior_close`。不得把当日 postclose daily partition 当作唯一观察窗口。盘中 quote clock 来自当日当前连续竞价时段内已完成的 `cn.dataset.rt_min` 五分钟槽（`decision_as_of` 及以前的最后一根，bar-end 如 10:05 或其同根 bar-start 如 10:00；10:05 oneshot 不得因只查 10:05 而丢掉已完成的 10:00 print）；开盘后、第一根五分钟 bar 完成前，允许使用该时段最早可证据槽（上午 09:30 / 下午 13:00 的 open print），不得用日线 close/touch 或上一时段 bar 冒充。缺钟与有钟必须分开；日线 close/touch 不得冒充 clock、bid/ask snapshot 或成交。真实 `rt_min` 行可以同时带 `trade_date` 与 `close`，只要 `time`/`bar_end` 对齐该槽或同根前一5分钟 print 就仍是 clock，不得当成日线 close。有钟之后还须绑定同一槽 `rt_min` bar 的成交量与 query receipt 才能形成 bar-evidence snapshot（last 为 mid，bid/ask 为 last±conservative slippage；收在 high/low 的 bar 仍可建 1-tick book，不得把 last/close 复制成两边），也不得用日线 close/touch 造 snapshot。缺 snapshot 仍是 `capital_fill_market_snapshot_unavailable`。同一连续竞价时段内的最后一根已完成五分钟 bar 对盘中 oneshot 是 fill-fresh；该 bar 尚未完成时，开盘 print 是最早可证据且同样 fill-fresh。fill snapshot 以 decision/execution 为 as-of，不得用 30s L1 报价窗把五分钟 bar 判 stale。午休与 14:57 后连续竞价已结束是 `paper_continuous_session_unavailable`；上午 bar 进入下午时段才是 `paper_market_snapshot_stale`。query receipt 失败不得把已经取回的 clock 行丢掉。`PAPER_FILLED` 只在 ledger `fill_commit` 之后。日历 `is_open=0` 继续 fail-closed。coverage 计数、日线 close/touch、`MinuteFixturePaperBook` 与 `compose_paper_runtime` 都不是成交。缺 window 或 quote clocks 不得发明 fill。`Ashare/sim_executor.py → shared/execution/sim_broker.py → shared/execution/local_sim_ledger.py` 仅是限时 legacy/compatibility 诊断链，不得作为 V1 fallback 或另一套资本/执行 authority。
- 只有实际 `filled/partial` 数量、价格、时区时间、5 分钟正成交量证据、候选/执行来源和完整 PIT lineage 才可进入 execution-eligible 样本。
- 买入通过 durable outbox 原子提交 `fill_commit`；卖出原子提交 `ashare_sell_commit`。capital commit 成功/幂等成功前不得把成交计入策略绩效。
- partial 只消费实际成交部分；终态原子释放未使用预约。pending commit 保守占用风险并在重启后重放。
- 盘后 MTM reconcile 必须以完整成交/持仓和 exact reservation manifest 验证现金、持仓、冻结额、outbox watermark 与 ledger head CAS。
- planning/risk/rebalance 前必须从 current A股 market capital ledger 建立唯一持仓 authority，并按 capital A → 全部 position sources → capital B 双读绑定。任一 source 缺 authority/generation/lineage/checksum/trade date/count/fingerprint，或规范化持仓不一致，统一 `capital_position_source_mismatch`；不得默认空仓、读取 legacy/strategy 后继续，或生成普通 8 仓容量拒绝。
- position authority validity 与 new-risk eligibility 分开：日亏/连亏/7% 回撤只阻断 buy/open/add，并保留 verified positions 供 sell/trim/exit 的 T+1、幂等、成交与 `ashare_sell_commit`；authority/source 失效时才全阻断。
- 资金计划输出 `deployed_utilization_rate`、`committed_utilization_rate`、`planned_stock_utilization_rate`、`undeployed_capital_cny`、`undeployed_reasons`、`position_capacity` 和 `remaining_position_slots`。
- 204001 等现金管理只作人工建议，`auto_order=false`，收益归入 `cash_management_yield` 并排除于股票 alpha。

## 事实与成熟度

- 样本 authority：`shared/review/ashare/sample_journal.jsonl`。
- 投影：`sample_kpi_latest.json`、`evolution_decision_latest.json`、`market_maturity_latest.json`；可重建，不能反写事实。
- SampleJournal/KPI 是唯一演化 authority。旧 review/portfolio 文件不得触发自动生命周期或风险晋级。
- 第 5、10 个交易日仅是自动证据/报告检查点，不是人工晋级或模拟接入门禁；缺少成熟度证据不能阻止数据合格股票继续积累模拟样本。全程保持 sim-only，成熟度通过也不自动实盘。
- 未来 20%–30% 人工试运行必须由 Nicholas 另行明确确认。邮件/同花顺路由未实现，不属于本模块当前入口。

运行、验收和回滚见 [../docs/operations.md](../docs/operations.md)，字段见 [../docs/data_contract.md](../docs/data_contract.md)。
