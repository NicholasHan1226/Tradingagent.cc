# TradingAgent 项目规则

> 阅读顺序：本文件 → [STATUS.md](STATUS.md) → [docs/AGENTS.md](docs/AGENTS.md)。跨仓修改还需读取 Finance 工作区和目标仓最近层 `AGENTS.md`。

## 项目定位

- TradingAgent 负责候选、预测、组合决策、风险门禁、模拟执行、样本、复盘和只读看板。
- SharedSignals 是基础数据 authority；生产只通过其 HTTP API 消费，不直读兄弟仓数据库，也不在本仓现场采集行情。
- MarketGraph 是可选只读研究增强。它不是价格、资本、账户或执行 authority，`mg_off` 必须能独立形成样本闭环。
- 当前目标是验证工程闭环、样本质量、费用/滑点后结果与回撤；不承诺盈利，更不承诺稳定盈利。

## 当前唯一资本事实

- A股和 CNFutures 各有一个独立、fresh-start、50,000 CNY 的 simulated authority：`ashare-capital-v1` 与 `cn-futures-capital-v1`，当前 `authority_generation=1`。
- 两个账户的现金、持仓/保证金、预约、盈亏、回撤、风控、execution lineage 和样本归因完全分离。总览只可并列，禁止相加、净额抵消或互相补资。
- A股政策：股票总敞口上限 90%（45,000 CNY），单一标的累计上限 15%（7,500 CNY），100 股整手，组合容量 8 且至少支持 7 个不同股票；全部 50,000 CNY 有资格服务合格机会，但不强制满仓。
- CNFutures 政策：保证金使用率上限 50%（25,000 CNY）。保证金容量和止损损失预算分开验证，不能把保证金上限当作可承受亏损。
- 每个市场独立执行：日亏 3% 暂停、连续亏损 3 次暂停、回撤 5% 仅收紧风险预算至 0.75 倍、回撤 7% 才暂停并复核。
- 政策源仅为 `shared/capital/ashare_capital_policy.yaml` 和 `shared/capital/cn_futures_capital_policy.yaml`。调用方不得复制另一套漂移常量。
- 旧共享资金池、旧模拟持仓/PnL、旧多账本与历史账户只读冻结，不导入、不迁移、不进入新统计；退役入口不得恢复。

## Simulation-only 红线

- `REAL_TRADING_ENABLED=false`；当前记录必须是 `capital_layer=simulated`、`account_type=simulated`、`real_trading_enabled=false`。
- 任一真实资金、live broker、direct execution、真实账户或签名密钥标记必须 fail closed，不能静默降级为 simulated/shadow。
- A股首 1–2 周只跑模拟；第 5、10 个交易日是人工复核点，不是自动实盘日期。
- 自动 champion 晋级、自动风险扩张和自动 live transition 永久关闭。即使 `promotion_evidence_ready=true`，也只表示证据检查通过，不构成授权。
- 未来 A股只可能在 Nicholas 明确确认后进入 20%–30% 初始敞口的人工试运行。拟议“TA 信号 → 邮件 → Nicholas 在同花顺人工复核下单”仍是外部设计，未在本仓实现；不得发送邮件或连接券商。
- CNFutures 长期模拟，无实盘日期，不绑定 A股进度。

## A股样本与组合执行

- 对所有数据合格候选保存 observation/counterfactual prediction。成熟阈值或执行门禁不能阻断 observation；数据不可靠时才拒绝标签。
- 初始正交假设族仅为：趋势突破/强势延续、回调/短反转、事件催化+价格确认、防御低波/空仓基线。风格只有预测与影子归因，没有独立资金或订单 authority。
- 同一 immutable base snapshot 必须生成 paired `mg_on` / `mg_off`；`mg_off` 不得读取 MG 特征。
- Exploration 使用安全 top-K 内分层随机/epsilon-greedy，记录 policy、seed、selection probability/propensity；每日最多新增 1 个探索头寸，探索累计敞口上限 7,500 CNY，探索日亏上限 225 CNY。
- Exploration 只可降低分数、最小 edge、研究完整度等策略门槛；数据、价格/成交证据、流动性、时段、T+1、整手、资金、幂等、累计敞口、日亏、连续亏损、回撤和实盘隔离永不放宽。
- Exploitation 保留成熟门槛。多个风格由一个组合决策器解决冲突、相关性、资金和幂等；同一股票同日只产生一份真实规格模拟订单。
- 成交保存 `primary_style`、`supporting_styles`、`style_scores`、`style_versions`、`decision_policy_version`、风格争议和 sample intent；未选风格仍生成标签。
- 资金计划必须输出 deployed/committed/planned utilization、dynamic operating cash、undeployed capital 和具体 undeployed reasons。现金管理收益与股票 alpha 分账，不得伪造资金利用率。

## CNFutures 样本与执行

- 每个有效交易会话至少保留 prediction/candidate/hold/risk reject/simulated fill 之一，并保存会话、合约、方向、原始 ranking score、市场状态、MG 状态和未交易原因。
- 原始 heuristic score 与 uncalibrated expected-return prior 必须按原名保存，不能称为未来收益概率或已校准预期收益。
- 最小一手、真实规格保证金、手续费、滑点、价格限制、会话、夜盘跳空、换月和风险预算全部适配时才可 `execution_eligible`；否则 quantity=0、`counterfactual_only=true`，方向预测和标签继续。
- 不得为样本绕过最小一手、保证金、夜盘、连续亏损、日亏、回撤或持仓/预约一致性。
- 静态合约规格只作模拟 bootstrap；没有实时可追溯规格时，不得声称交易所级撮合、保证金或强平精度。

## 资本与执行原子性

- 每个市场各自使用 append-only capital ledger、stable reference、checksum chain、PIT lineage、head CAS 和 exact reservation manifest。
- A股买入 `fill_commit`、A股卖出 `ashare_sell_commit`、期货开仓 `fill_commit`、期货平仓 `position_close_commit` 必须使用实际数量、价格、费用/滑点和不可变成交/持仓指纹。
- 成交事实先进入 durable outbox；资本 commit 成功或幂等重放成功后才可标为 execution-eligible。pending outbox 保守占用风险，重启后重放，不能伪造释放或重复记账。
- partial fill 只消费实际部分，未成交部分在终态原子释放；同一 symbol 的持仓市值 + 未决预约 + 新订单合并校验 15% 上限。
- 当日 MTM reconcile 必须证明现金、持仓/保证金、冻结额、exact reservations、未结 fill commits 和 execution lineage 一致；风险以 MTM equity 而非仅 realized PnL 计算。

## 样本、标签与演化 authority

- A股唯一演化事实源是 append-only `shared/review/ashare/sample_journal.jsonl`；`sample_kpi_latest.json`、evolution decision 和 maturity 都只是可重建投影。
- 样本必须分层：observation/counterfactual、exploration fill、exploitation fill、completed round trip、exit/stop、risk reject、chain validation，禁止混算。
- horizon 固定为 `m30/m60/close/1d/3d/5d`。标签使用 PIT `as_of`，真实成交采用实际费用/滑点；反事实标签采用版本化保守成本。
- 5 分钟重复样本聚类去重；选择概率、预测快照、source SHA、execution lineage、actual costs 和成交重验证缺失时不得进入晋级证据。
- SampleJournal/KPI 是唯一演化 authority。旧 portfolio/weekly/legacy review 不得自动给出生命周期或风险晋级。
- “样本不足”不能单独导致长期零 observation 或零交易；无探索成交必须归因于无数据合格候选或具体安全门禁。

## 活跃写入与前端

- A股资本：`shared/logs/capital/ashare/`
- CNFutures 资本：`shared/logs/capital/cn_futures/`
- A股 server-local 执行：`shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/`；`shared/logs/local_sim/` 只读冻结。
- 执行状态与回执：`signals/`
- A股样本与复盘：`shared/review/ashare/`
- `front/` 是唯一活跃只读前端。All Markets 只可汇总非货币计数；不同市场的资本、权益、PnL、收益率和回撤绝不聚合。

## 验收与发布边界

- 命令、运行顺序和回滚见 [docs/operations.md](docs/operations.md)；字段见 [docs/data_contract.md](docs/data_contract.md)；样本与成熟度见 [docs/capital_growth_validation.md](docs/capital_growth_validation.md)。
- 当前事实只写 [STATUS.md](STATUS.md)。文档不得把本地测试、GitHub、生产文件、生产 runtime、cron、真实市场样本或真实交易混成一个“完成”。
- 回滚只能停止新任务、切回已验证代码并保留 append-only 事实；不得删除/改写新账本，也不得恢复旧共享账本。
- 未经单独授权，禁止 commit、push、deploy、apply cron、发邮件、操作 GUI 或接入真实交易。
