# TradingAgent / Ashare

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 定位与账户

- 本模块负责 A股候选、多风格预测、组合资金计划、T+1、server-local 模拟执行和样本归因；不采集行情，也不连接真实券商。
- 唯一执行账户是 fresh-start `ashare-capital-v1` / generation 1 的 50,000 CNY simulated 账户。
- 资本 ledger 位于 `shared/logs/capital/ashare/`，执行事实位于 `shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/`；旧 `shared/logs/local_sim/` 冻结，adapter 资金只可诊断。
- 股票总敞口上限 45,000 CNY，单票累计“持仓市值 + pending reservations + 新订单”上限 7,500 CNY，100 股整手；容量 8 并至少支持 7 个不同股票。
- 不设固定保护现金、不强制满仓。费用、滑点、冻结额与 pending order 构成动态运营现金；计划必须写利用率和未部署原因。
- 历史共享资金池、旧持仓/PnL、旧多账本只读冻结，不导入或计入当前统计。

## 样本顺序

1. 对所有数据合格候选生成 observation/counterfactual prediction。
2. 以同一 immutable base snapshot 生成 paired `mg_on` / `mg_off`，后者不得含 MG 特征。
3. 四类正交风格保存 thesis、horizon、raw ranking score、uncalibrated prior、风险请求和 abstain/reject reason。
4. Exploitation 走成熟门槛；需要采样时，Exploration 从安全 top-K 进行分层随机/epsilon-greedy并记录 propensity。
5. 单一组合决策器处理风格冲突、相关性、资金和幂等，同一股票同日最多一份订单。
6. 保存 prediction/fill/reject/round trip/chain validation，并生成 `m30/m60/close/1d/3d/5d` 标签。

初始假设族仅为趋势突破/强势延续、回调/短反转、事件催化+价格确认、防御低波/空仓基线。不要用轻微阈值变化扩张风格。

## Exploration 边界

- 每日最多新增 1 个探索头寸；探索累计敞口上限 7,500 CNY；探索日亏上限 225 CNY。
- 只可下调候选分数、最小 edge 或研究完整度等策略门槛。
- 数据来源/新鲜度、普通 A股与流动性、真实价格/成交证据、交易时段、T+1、涨跌停、100 股整手、现金/持仓、幂等、累计敞口、日亏、连续亏损、回撤和实盘隔离永不放宽。
- “样本不足”不能单独导致零 observation 或零交易；无 exploration 必须记录无合格候选或具体硬门禁。

## 执行与资本一致性

- 本地执行链是 `Ashare/sim_executor.py → shared/execution/sim_broker.py → shared/execution/local_sim_ledger.py`。
- 只有实际 `filled/partial` 数量、价格、时区时间、5 分钟正成交量证据、候选/执行来源和完整 PIT lineage 才可进入 execution-eligible 样本。
- 买入通过 durable outbox 原子提交 `fill_commit`；卖出原子提交 `ashare_sell_commit`。capital commit 成功/幂等成功前不得把成交计入策略绩效。
- partial 只消费实际成交部分；终态原子释放未使用预约。pending commit 保守占用风险并在重启后重放。
- 盘后 MTM reconcile 必须以完整成交/持仓和 exact reservation manifest 验证现金、持仓、冻结额、outbox watermark 与 ledger head CAS。
- 资金计划输出 `deployed_utilization_rate`、`committed_utilization_rate`、`planned_stock_utilization_rate`、`undeployed_capital_cny`、`undeployed_reasons`、`position_capacity` 和 `remaining_position_slots`。
- 204001 等现金管理只作人工建议，`auto_order=false`，收益归入 `cash_management_yield` 并排除于股票 alpha。

## 事实与成熟度

- 样本 authority：`shared/review/ashare/sample_journal.jsonl`。
- 投影：`sample_kpi_latest.json`、`evolution_decision_latest.json`、`market_maturity_latest.json`；可重建，不能反写事实。
- SampleJournal/KPI 是唯一演化 authority。旧 review/portfolio 文件不得触发自动生命周期或风险晋级。
- 第 5、10 个交易日只标记人工复核到期。首 1–2 周保持 sim-only；成熟度通过也不自动实盘。
- 未来 20%–30% 人工试运行必须由 Nicholas 另行明确确认。邮件/同花顺路由未实现，不属于本模块当前入口。

运行、验收和回滚见 [../docs/operations.md](../docs/operations.md)，字段见 [../docs/data_contract.md](../docs/data_contract.md)。
