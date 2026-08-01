# TradingAgent / CNFutures

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 定位与账户

- CNFutures 负责国内期货方向判断、会话/合约语义、一手 affordability、模拟成交和复盘。
- 唯一资金 authority 是独立 fresh-start `cn-futures-capital-v1` 的 50,000 CNY simulated 账户；generation 必须在每轮从 current authoritative snapshot 读取且为正数，不得写死为 1，也不与 A股共享、相加、净额或互补。
- 保证金使用率上限 50%，当前 25,000 CNY。保证金容量与单笔止损损失预算是两道独立门禁。
- 日亏、连续亏损、MTM 回撤和 high-water 各自独立；5% 回撤收紧预算，7% 暂停。
- 当前长期模拟，无实盘日期。SimNow/CTP 只是测试或未来预留，不构成真实交易接入。

## 每会话样本

- 每个有效会话至少保留 `prediction`、`candidate`、`hold`、`risk_reject` 或 `simulated_fill` 之一。
- 记录 trade date/session、symbol/product、style/version、方向、raw heuristic score、uncalibrated prior、market regime、MG 状态、holding horizon、未交易原因和标签状态。
- 原始 score/prior 不得命名为已校准胜率或未来收益概率。
- 闭市、午休、日盘后等待夜盘、品种夜盘结束、换月保护和资金拒绝是可复盘状态，不应混成系统 error。

## Counterfactual 与 execution-eligible

- 方向预测与当前本金可执行性分开。
- multiplier、tick、最小一手、可追溯保证金、手续费、价格限制、滑点、夜盘跳空、合约/换月、会话、持仓和风险预算全部适配时，才可 execution-eligible simulated fill。
- 任一不适配时 `quantity=0`、`counterfactual_only=true`，保存方向预测、具体拒绝原因和后续标签。
- 不得为了样本绕过最小一手、保证金、止损预算、夜盘、换月、连续亏损、日亏、回撤或重复敞口。
- 静态 `contract_rules.py` 只作模拟 bootstrap；缺少实时可追溯规格时不得声称交易所级保证金、盘口撮合或强平精度。

## 原子资本闭环

- 开仓预约使用最坏情形 margin + fee，绑定 authority/generation、execution lineage、PIT timestamp、source SHA、risk unit 和 stable reference。
- 实际开仓通过 durable outbox 提交 `fill_commit`；实际平/减仓提交 `position_close_commit`。两者使用 actual fill、actual fee、actual margin/realized PnL、receipt/local-position fingerprints 和 ledger-head CAS。
- commit 成功或幂等重放成功后才更新 execution-eligible 绩效；pending action 保守阻断新增风险并可 crash replay。
- partial 只处理实际数量；不能把原订单数量、请求价格或旧 reservation 当成交事实。
- 当日 MTM reconcile 必须证明现金、持仓数量/成本、保证金、冻结额、exact reservations、未结 commit IDs 和 execution lineage 一致。

## 订单事件与复核指标

- `signals/order_events/cn_futures_order_events.jsonl` 是 CN 模拟订单生命周期的 append-only 事件证据，checksum chain 生成 `cn_futures_order_projection.json`；启动时必须与 `pending/claimed/running/filled/partial/...` 目录投影 reconcile。不一致时只暂停新增模拟执行并继续 observation/counterfactual 采样。
- 当前本地模拟器是 IOC-like，`partial` 明确记录 `terminal=true`。事件 schema 保留显式 terminal 与 `ACTIVE/REDUCING/HALTED` 生命周期，供未来异步 adapter 设计；当前未实现 broker、邮件、同花顺或实盘订单续报。
- `realized_pnl/(drawdown+fee)` 只可命名为 `net_pnl_to_drawdown_plus_fee_ratio` 诊断值，不能称为 Sharpe。没有同频净收益序列时 `sharpe=null`、DSR 不可用，且该值不得进入晋级证据或自动调权。

## 数据与成熟度

- 盘中合约与 5 分钟行情只消费 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；fresh handoff 前只允许显式 fixture/mock。CNFutures 当前链不得读取 TradingDatas SQLite，也不得回退到旧 `SHAREDSIGNALS_API_URL` reader、`/tushare`、`/source_status` 或 provider 专用 route；历史诊断代码不构成可恢复入口。
- 当前可运行模拟 universe 仅为豆粕 `M`；螺纹钢 `RB` 只能做只读影子评估，不能生成模拟成交。它不满足未来生产的多品种覆盖要求，故不得据此晋级；扩大范围须经人工审阅并更新 [STRATEGY_ARCHITECTURE.md](STRATEGY_ARCHITECTURE.md)。
- 成熟度独立展示有效样本、完整回合、品种/波动/会话覆盖、夜盘、换月、极端风险、费用后结果、回撤和稳定性；不读取 A股模拟天数或晋级状态。
- 自动晋级、自动风险扩张和 live transition 均关闭。

会话验收见 [../docs/operations.md](../docs/operations.md)，字段见 [../docs/data_contract.md](../docs/data_contract.md)。
