# TradingAgent 项目规则

> 阅读顺序：本文件 → [STATUS.md](STATUS.md) → [docs/AGENTS.md](docs/AGENTS.md)。跨仓修改还需读取 Finance 工作区和目标仓最近层 `AGENTS.md`。

## 项目定位

- TradingAgent 负责候选、预测、组合决策、风险门禁、模拟执行、样本、复盘和只读看板。
- SharedSignals 是基础数据 authority；生产只通过其 HTTP API 消费，不直读兄弟仓数据库，也不在本仓现场采集行情。
- MarketGraph 是可选只读研究增强。它不是价格、资本、账户或执行 authority，`mg_off` 必须能独立形成样本闭环。
- 当前目标是验证工程闭环、样本质量、费用/滑点后结果与回撤；不承诺盈利，更不承诺稳定盈利。

## 当前唯一资本事实

- A股和 CNFutures 各有一个独立、fresh-start、50,000 CNY 的 simulated authority：`ashare-capital-v1` 与 `cn-futures-capital-v1`。generation 1 只是历史 fresh-start 基线；消费者每轮必须读取、验证并传播 current snapshot 的正整数 generation，禁止写死。
- 两个账户的现金、持仓/保证金、预约、盈亏、回撤、风控、execution lineage 和样本归因完全分离。总览只可并列，禁止相加、净额抵消或互相补资。
- A股政策：股票总敞口上限 90%（45,000 CNY），单一标的累计上限 15%（7,500 CNY），买入100股整数倍；卖出只允许100股整数倍、完整不足100股余额或全部退出，且受T+1可卖量约束。组合容量 8 且至少支持 7 个不同股票；全部 50,000 CNY 有资格服务合格机会，但不强制满仓。
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

- V1 第一阶段只允许一个冻结、可解释的 `uncalibrated_deterministic_rank_score` Champion 与现金基线进入组合；rank score 不得称为概率、期望收益或投资建议。
- 对所有数据合格的主板候选保存 observation/counterfactual 与后续标签请求；风险、成本或资金门禁可以拒绝订单，但不能抹掉候选和拒绝原因。数据/PIT 不可靠时标签必须 fail closed。
- `OpportunityRadar`、append-only OpportunityLedger、多期限预测合同和三风格路由已是本地隔离的 shadow candidate；它们只能做 PIT 合同、fixture、反事实和研究审计，不证明预测有效、概率已校准或 runtime 已接入，也不得改变 V1 Champion 排名、仓位、风险或订单。
- 仓库既有四风格、exploration/exploitation 与旧组合路径属于 time-boxed legacy/历史能力，不能成为 V1 隐式 fallback、第二资金 authority 或当前完成证明；其消费者按 `legacy_inventory.yaml` 分批迁移并同批删除。
- MarketGraph 消融只能在同一 immutable base snapshot 上生成 paired `mg_on` / `mg_off` 研究证据；`mg_off` 不得读取 MG 特征，任何一侧均无资金或订单 authority。
- 同一股票同日最多一份 authority-bound 模拟订单；计划必须经无默认`AccountAuthorityVerifier`复核完整模拟账户内容、持仓、mark、现金、gross、T+1可卖量、proof有效期，再绑定`cost_policy_id`、独立复算费用、现金顺序和drift constraint。持久漂移latch的风险乘数与动作严重度只允许保持或收紧；本地候选在每次risk评估和网络关闭的模拟副作用前重读最新latch，未来live broker仍须在真实外部副作用前完成同等复核。
- LLM source span 永远按不可信引用数据处理；已知提示注入模式、敏感载荷、未验证artifact/authority或未知引用必须在transport前阻断。模式门不能声称覆盖全部语义/编码攻击；typed provider receipt、local CAS journal和`.head`完整性锚点不能冒充真实DeepSeek transport、外部密封或durable production receipt authority。LLM永远无候选、排名、仓位、风险、订单或账户authority。
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
- horizon 固定为 `m30/m60/close/1d/3d/5d`。标签使用 PIT `as_of`，真实成交采用实际费用/滑点；反事实标签采用版本化保守成本。A股`ValidationPlan`必须经无默认calendar verifier生成detached proof并在预测前冻结；`close/1d/3d/5d` target只可从该会话authority派生，缺目标会话证据不得顺延。该本地proof只证明目标时刻绑定，不等于生产calendar、exit price、总回报或公司行动真值；缺真实market-truth/OOS/adjustment authority不得发布predictive evidence。
- 5 分钟重复样本聚类去重；选择概率、预测快照、source SHA、execution lineage、actual costs 和成交重验证缺失时不得进入晋级证据。
- SampleJournal/KPI 是唯一演化 authority。旧 portfolio/weekly/legacy review 不得自动给出生命周期或风险晋级。
- “样本不足”不能单独导致长期零 observation 或零交易；无探索成交必须归因于无数据合格候选或具体安全门禁。

## 活跃写入与前端

- A股资本：`shared/logs/capital/ashare/`
- CNFutures 资本：`shared/logs/capital/cn_futures/`
- A股 server-local 执行：由 verified current capital snapshot 的 `execution_lineage_id` 派生 `shared/logs/execution_lineages/<execution_lineage_id>/`；固定日期 lineage 与 `shared/logs/local_sim/` 仅可作历史审计。
- `signals/` 是旧执行队列兼容路径；V1 fixture/day-loop 使用显式隔离 root，不得把它当成 current authority fallback。
- A股样本与复盘：`shared/review/ashare/`
- `front/` 是唯一活跃只读前端。All Markets 只可汇总非货币计数；不同市场的资本、权益、PnL、收益率和回撤绝不聚合。
- 系统仅供 Nicholas 个人内部使用。前端与只读 API 默认只监听 localhost；`tradingagent.cc` 可作为个人远程入口，但必须由 Cloudflare Access 或等价单用户认证保护。禁止匿名公网访问或直接暴露 API，远程入口必须独立完成权限、路由和撤销验证。

## 验收与发布边界

- 命令、运行顺序和回滚见 [docs/operations.md](docs/operations.md)；字段见 [docs/data_contract.md](docs/data_contract.md)；样本与成熟度见 [docs/capital_growth_validation.md](docs/capital_growth_validation.md)。
- 当前事实只写 [STATUS.md](STATUS.md)。文档不得把本地测试、GitHub、生产文件、生产 runtime、cron、真实市场样本或真实交易混成一个“完成”。
- 回滚只能停止新任务、切回已验证代码并保留 append-only 事实；不得删除/改写新账本，也不得恢复旧共享账本。
- 未经单独授权，禁止 commit、push、deploy、apply cron、发邮件、操作 GUI 或接入真实交易。
