# TradingAgent

> **阅读顺序：** 进入 TradingAgent 后，按以下顺序阅读：
> 1. 本文件 — 理解 TradingAgent 的规则、边界和运行时护栏
> 2. **[STATUS.md](STATUS.md)** — 理解当前状态、已知问题、下一步任务
> 3. 跨系统协作前，读 [根目录 AGENTS.md](../AGENTS.md) 和 [根 STATUS.md](../STATUS.md) 了解三系统架构和全局状态

## 三系统定位

- TradingAgent 是交易判断和任务队列系统：读取 SharedSignals 的市场/事件数据与 MarketGraph 的研究证据，生成影子盘、模拟盘、实盘复核信号、日报和周报。
- 三系统当前不是 MCP 互调，也不是强耦合单体；SharedSignals 供数，MarketGraph 供研究图谱，TradingAgent 做交易判断、队列、回执汇总和通知。
- 未来系统间调用优先通过公开服务接口、只读 API/MCP read model 或明确数据契约完成；TradingAgent 不应直接依赖 MarketGraph 的内部实现细节，也不应把 MarketGraph 当执行入口。

## 目标

交易模拟盘/影子盘，高频训练策略，每日 2 次复盘。

## 代码位置

- 生产路径：`/opt/investment/tradingagent/`
- 开发路径：`/Users/nicholashan/Projects/Finance/TradingAgent/`
- 生产前端/运营看板：`front/`，同属 TradingAgent 仓库，是唯一活跃前端入口；不要使用兄弟级 dashboard 仓库作为生产或开发来源。
- 工具集：`/opt/investment/tools/`（约 20 个工具）

## 架构边界（永久规则）

### 执行桥

A 股模拟盘默认闭环走服务器本地 paper fill 与统一模拟账本：`job_ashare_sim_exec → Ashare/sim_executor.py → shared/execution/sim_broker.py → shared/logs/sim_ledger/ashare`。

- Hermes/mini 是 A 股同花顺 GUI 执行桥的预留第二路径，只有显式设置 `ASHARE_SIM_HERMES_ENABLED=1` 时才投递到 `signals/pending` 并要求 mini 回执。
- Hermes/mini 只执行和回写，不做买卖判断；当前服务器本地模拟闭环不依赖 Hermes 可用性。
- `~/Desktop/Investment` 不再是 active dev root 或 live runtime root；Mac Mini live runtime 使用 `~/.hermes/ashare-runtime`，服务器写回使用 `/opt/investment/tradingagent/signals`。
- 旧桌面路径任务 `ai.hermes.sim-remote-sync` 与 `ai.hermes.condition-cleanup` 已于 2026-07-02 禁用；不得重新启用，除非先确认新的事实源、回滚方式和验证方式。
- MarketGraph 不得直接触发 Hermes/Mac Mini/同花顺或任何执行 webhook。
- 执行桥归 TradingAgent。

### 数据流

- SharedSignals 是独立供数层：定时采集/维护先沉淀数据，TradingAgent 通过 reader/read model 按需读取。
- 生产运行时必须设置 `SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`；`TradingagentDataReader` 默认通过 SharedSignals/ShareChannel API 取数，SQLite 仅是只读降级路径。
- TradingAgent 不应在每次交易判断时重新现场采集 Tushare。
- 跨系统写入必须走明确数据契约，不把一个系统目录当作另一个系统的内部模块直接改写。

## 关键运行时护栏（永久规则）

### 订单幂等与队列隔离

- A股模拟订单同日幂等：`market + account + trade_date + symbol + side`。
- 发单前检查所有状态的 signal cards（`pending|claimed|running|filled|failed|partial|expired|cancelled`），重复同日同标的记 `status=duplicate`，不入新订单。
- 迁移测试、拒绝测试和生产模拟交易任务必须隔离，避免测试 pending 导致生产调度 `skipped=mini_busy`。

### 候选池过滤

- A股候选池动态 5 层结构（holdings/watch/candidate/universe/fundamental），每次调度动态重建。
- 所有 A股入口必须只保留普通 A股代码段；`200xxx.SZ`、`900xxx.SH`、北交所等非本模拟执行链路标的必须在三层被过滤：`Ashare.adapter`、`shared/screening/universe_filter.py`、`shared/screening/candidate_pool.py`。
- 可执行候选必须有近期日线 close > 0 和成交额/流动性证据；无名称、无日线覆盖、无流动性证据的股票在 universe/candidate 阶段排除。
- A股 simulated 新买入只允许来自 candidate 层；watch 只能观察，holdings 只参与持仓/卖出/换仓评估，候选池为空或 candidate pool 异常时必须 fail-closed 为无交易，不能回退到顺序 universe 或资产表样本。
- A股 simulated 订单必须携带来源字段：买入为 `candidate_pool_layer=candidate`、`execution_source=ashare_candidate_layer`；卖出/压缩为 `execution_source=ashare_rebalance_sell`，便于复盘确认成交来源。
- A股 auto pipeline 不得用 `price=1.0` 作为候选或执行信号兜底；缺真实分钟/日线价格时跳过该候选或信号。
- 组合构建前过滤 `price <= 0`，记录到 `skipped_candidates`。
- Tushare daily `amount` 按千元口径存储，流动性比较前必须换算为元。

### Mini/Hermes 健康门

- `job_ashare_sim_exec` 默认不检查 mini health，也不写 Hermes pending；A 股模拟单在服务器内完成 paper fill、账本和复盘数据闭环。
- 仅当 `ASHARE_SIM_HERMES_ENABLED=1` 时，任务才启用 mini health/backpressure 检查，并把同一模拟信号投递给 Mini/Hermes/同花顺模拟盘作为第二执行路径。
- Hermes 路径启用后，mini `/health` 不可用、`halted=true` 或 `pending + in_progress > ASHARE_SIM_MINI_BUSY_LIMIT`（默认 0）时，不得阻断服务器本地模拟闭环；任务必须记录 `mini_health_unavailable` / `mini_halted` / `mini_busy`，临时设置 `ASHARE_SIM_WEBHOOK_ENABLED=0`，继续写服务器本地 paper fill。
- 服务器本地模拟账本是训练/复盘数据权威来源；Hermes/mini 成功或失败只用于同花顺 GUI 模拟盘对照，仍以回执和截图确认为准。
- Hermes/mini 点击提交但没有严格持仓表/委托/成交确认时，写 unconfirmed failed receipt，创建 `executor_halt.json`，停止消费队列。截图确认只看裁剪后的持仓表区域。
- 同花顺模式识别：用 AX 标签确认"模拟"，不依赖 Vision 判断资金/账户区域。显式真实风险标识是"实盘"、"资金账号"、"普通交易"、"融资融券"。

### 交易时段保护

- A股模拟执行受交易时段保护：非工作日或非 `09:30-11:30` / `13:00-14:57` 直接 `skipped=market_closed`。

### 影子盘隔离

- 影子信号只写入 `signals/shadow/pending`，不进入可执行队列。
- `signals/shadow` 具备完整状态目录：`pending/claimed/running/filled/expired/cancelled/failed/partial`。
- PM/Crypto/US/HK 的影子和模拟工具必须拒绝 `real_money_enabled`、`live_broker_enabled`、`direct_execution_enabled` 以及订单/账户/配置中的 `capital_layer=real`、`account_type=real`、签名密钥或 live broker 标记；不得把真实执行负载静默改写成 simulated 后继续执行。
- A股影子账本拒绝非普通 A股代码（200xxx.SZ 等）。
- 影子盘估值优先 SharedSignals 日线收盘价，缺失时回退最近影子成交价。

### 实盘安全门

- 实盘队列只能使用 `signals/real/*`，不得写入 shadow/sim 队列，也不得直接写入当前 A股模拟执行队列。
- `REAL_TRADING_ENABLED` 默认关闭；任何实盘订单、实盘队列提交或人工确认流程在开关未显式启用时必须拒绝。
- 实盘订单必须先通过手工确认 token、单笔/单日资金硬上限、A股交易时段、T+1、emergency halt 文件检查；任一失败必须抛 `SafetyViolation`，不能降级为 simulated/shadow。
- `signals/real/pending` 仍是人工确认后的隔离队列；不得被视为自动下单、自动点击或已成交证明。成交状态只接受带 `receipt_sha256`/`checksum`/`sha256` 校验的回执。

### LLM/DeepSeek 使用边界

- A股 5 分钟级模拟执行调度默认 `TRADINGS_DEBATE_MODE=fast`，用六维分数生成确定性 belief_score，不阻塞等待 DeepSeek。
- DeepSeek/LLM 只用于研究层、多空复核、日报/周报和慢速校准。

### PM 调度

- PM shadow scan 每 10 分钟运行；`run_job` 锁防止并发。
- `job_pm_optimize` 运行产物写入 `shared/review/pm/`，不写入 Git 跟踪路径。

### 风格演化状态

- `Crypto/`、`PM/`、`US/`、`HK/` 下的 `styles/*.json` 是只读基础配置，不作为运行时状态存储。
- 自动演化只能把权重、暂停/降级状态、performance、comparison 和自动生成 variant 写到 `shared/review/<market>/` 或生产运行层对应 review root。
- `shared/review/<market>/style_weights.json` 是运行时风格权重/状态来源；自动生成风格放在 `shared/review/<market>/generated_styles/`。
- 不得让 cron 或 pipeline 回写基础 `styles/*.json`，否则会污染 Git 工作树并混淆配置与运行结果。

### 回执完整性

- 服务器写入 receipt 前验证 `receipt_sha256`/`checksum`，不匹配拒写。
- 无签名的历史 receipt 标记 `unsigned`，不当作篡改或失败。
- `payload_sha256` 是下发指纹，不是回执签名；只有 `receipt_sha256`/`checksum`/`sha256` 用于判断 signed。

### 邮件通道

- 交易通道：`notice@tradingagent.cc → tradingadviser@coze.email`
- 系统通道：`notice@tradingagent.cc → soc@coze.email`
- 发送方式：Cloudflare Email Service REST endpoint
- 生产 env 入口：`/opt/marketgraph/.env` 保存 `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_EMAIL_API_TOKEN` 和 `EMAIL_FROM_/EMAIL_TO_`；loader 会兼容旧 `CF_EMAIL_*` 与 `EMAIL_*_FROM/TO` 命名，但文档和新增配置必须使用规范名。

## 服务器

- 主服务器：`8.138.181.177`（阿里云华南3/广州）
- 生产路径：`/opt/investment/tradingagent/`
- Mini 远程访问：Tailscale `100.125.4.113` / SSH alias `macmini-tailscale`
- `192.168.5.2` 是 RSS 服务器，不是 MarketGraph 主服务器，也不是 Hermes mini 执行桥。

## 复盘节奏

- 两次主复盘：11:45 午盘复盘、15:30 收盘复盘
- 22:00 夜间校准（汇总研究、归因、回测、尾盘候选和次日计划）
- 07:30 晨报（不计为复盘迭代）
- 尾盘候选扫描：14:40/14:50/14:56 生成 `MarketGraph/outputs/ashare_closing_buy_candidates.json`（仅观察，不入实盘）

## 关键命令入口

- A股市场健康检查：`PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/market_health.py --market ashare --pretty`
- 运维报告：`PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/ops_report.py --send-on never --pretty`
- 失败归档：`PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/archive_reviewed_signals.py --apply --batch-id <id> --reason <reason>`

## 工作区同步规则

- 仓库地址、remote 名称和默认分支以本仓库内 `git remote -v`、`git branch --show-current` 为准。
- 开发前检查 `git status -sb`、`git remote -v`、当前分支和是否落后远端。
- 工作树不干净时先判断改动来源，不得覆盖并发 agent、cron、桌面自动化或 Nicholas 的改动。
- 涉及交易 agent 行为、邮件/API、部署、配置、数据契约、风控边界、服务器路径、定时任务或协作流程的变更，必须同步更新核心文档。
- 涉及真实资金、实盘执行、账号凭据、2FA、私钥、邮件发送通道或生产服务器的操作，必须先说明授权边界、回退方式和验证方式。
- 提交时只暂存本次审计过的文件；数据库、缓存、日志、staging、密钥、本机运行产物和交易临时输出默认不提交。

## 历史事件日志

2026-07-01 发生了一系列运行时事件（虚假成交确认、过期 pending 清理、回执指纹闭环等），详细的**事件时间线、修复动作和事后复盘**记录在：[docs/runtime_incidents_20260701.md](docs/runtime_incidents_20260701.md)。

2026-07-02 发生了 Mini/服务器执行桥路径漂移与 `TradingagentDataReader` 导入回归，已记录在：[docs/runtime_incidents_20260702.md](docs/runtime_incidents_20260702.md)。该日志同时记录 `~/Desktop/Investment` 退役、旧 LaunchAgent 禁用、服务器修复提交和残余风险。

上述"关键运行时护栏"中的永久规则大部分是从这些事件中提取的。如果需要理解某条规则的背景或复盘某个事故链，查阅该事件日志。
