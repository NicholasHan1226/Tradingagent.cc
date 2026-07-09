# Tradings/CNFutures

## 目标

国内期货交易研究、全自动模拟盘和未来受控 CTP 接入预留。

## 约束

- 国内期货与 A 股分离: 保证金、杠杆、T+0、多空双向、夜盘、强平和合约换月规则不同。
- 当前阶段只允许模拟盘; 本模块不设单独影子盘层。未完成期货公司授权、穿透式监管确认、风控验收和 Nicholas 人工确认前, 不得自动实盘下单或撤单。
- CTP / SimNow 适配器只能作为模拟或测试接入; 不能把 SimNow 成交、回报或持仓描述成真实交易结果。
- 合约规则、保证金、手续费、夜盘、涨跌停、最小跳动和主力合约切换必须可追溯到数据来源; 未接入实时来源时只能使用显式静态规则。

## 执行

- 模拟盘: 本模块 `sim_executor.py` 只生成模拟成交回执和资金占用估算。
- 资金: 默认模拟本金为 200,000 CNY；可通过 `CN_FUTURES_SIM_CAPITAL_TIER=50000|100000|200000` 或 `default_sim_capital("cn_futures", capital_cny=...)` 使用 50,000 / 100,000 / 200,000 三档。非法档位回退 200,000 CNY。
- 风控拒单: 保证金 cap、风格暂停、会话不允许、换月保护等预期内风控结果必须写入 hold/risk rejection 原因；不得作为系统 `errors` 导致模拟任务 degraded。只有数据缺失、执行异常、无效价格或代码异常才应进入 error。
- 多风格验证: 通过独立模拟账户/策略风格并行记录, 不使用 `shadow_broker.py`。
- 只读 replay: `CNFutures/replay.py` 只能读取 SharedSignals 5分钟 bars 并回放现有风格触发情况，用于解释阈值、hold 原因和历史窗口表现；不得写订单、持仓、账本或实盘接口。Replay 必须复用 live 风格产品过滤，并标注 `execution_eligible` 与不可执行原因，尤其是产品不匹配、午休/闭市边界、保证金 cap、价格/合约规则缺失；历史 buy/sell 只能代表“当时风格触发”，不能直接代表当前可成交。
- 实盘: 未来通过 `shared/execution/` 下的受控网关抽象接入, 默认关闭。

## 边界

- SharedSignals 负责行情、合约和日历输入。
- MarketGraph 负责商品、宏观、跨市场研究证据。
- CNFutures 只消费上述输入, 负责期货市场内的订单语义、模拟成交、风控前置和执行状态。
- 盘中可交易合约池必须来自 SharedSignals API 的最新 `Futures` 5分钟批次；`fut_basic` 只作为合约元数据，不得作为盘中主 universe。
- 交易时段判断必须复用 `CNFutures/session.py`；午休 `11:30-13:00`、日盘后等待夜盘、非交易日等属于正常观察态，不能被开盘验收或健康检查误报为数据故障。
- 期货交易日与日历日不能混用；夜盘 21:00 后的模拟和健康检查必须使用 `CNFutures/session.py` 的活跃交易日。品种夜盘已经正常收盘后的最后一根 5分钟 bar（如铜 01:00）属于等待下一交易段，不得误报为 `stale_intraday_bar`。
- 开盘验收、实时健康和模拟盘巡检必须优先使用 SharedSignals API `/realtime_5min?market=Futures` 验证当前 5 分钟条线；SQLite read model 只允许显式诊断/测试开关下只读使用，不能作为生产自动兜底。
- 策略主动 `hold`、全部风格因夜盘不允许而空跑、保证金 cap 或换月保护等预期内门禁，应进入 pass/info 的可解释空跑；首样本验收不得因为“有 5分钟数据且策略主动 hold、但没有成交”而报警。只有数据缺失、实盘开关误启、成交缺 bar time、异常错误或应成交但无账本时才报警。
- 运维/看板健康输出必须把当前最新复盘与 append-only 历史累计分开：当前状态只看 `current`；旧的 `missing_intraday_bars`、`stale_intraday_bar`、历史风控分类等只能放在 `historical` 里用于复盘，不得和当前 live health 并列展示成今天故障。
