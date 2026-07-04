# TradingAgent 状态

> **给所有 agent：** 读完 [AGENTS.md](AGENTS.md) 理解规则后，读本文件理解"现在在哪、要去哪、能做什么"。
>
> **⚠️ 变更后必须更新本文件。**
>
> 最后更新：2026-07-04 (六维打分异常处理修复)

---

## 一、当前状态

- **A 股多风格模拟盘**：完整闭环运行（信号生成 → sim 账簿 → 复盘）；旧层已完全退役（0 文件、0 cron）
- **A 股模拟盘**：通过 Mac Mini Hermes 执行，收/发/回执链路已修复
- **执行桥**：Mac Mini `~/.hermes/` 下 Hermes 正常运行，只执行和回写，不做买卖判断；live runtime 为 `~/.hermes/ashare-runtime`，服务器回写为 `/opt/investment/tradingagent/signals`；mini live 脚本默认值也已改到新路径
- **PM（预测市场）**：多风格 simulated 扫描每 10 分钟运行；checked-in config 使用 USDC；PM sim/style 输出写入 `shared/review/pm/style_comparison.json`
- **多市场**：PM/Crypto/US/HK sim executor 和 config schema 已加真实执行拒绝；US/HK simulator 入口已拒绝真实 order/account payload，fill 结果不回显 account payload；共享安全扫描递归覆盖 `direct_execution`/`real_execution`/`live` 别名；Crypto/US/HK Phase D P0 工具已独立实现；US/HK P1 report/validation/promotion 工具已补齐；Crypto/PM P1 report/validation/promotion 工具已补齐；Crypto/US/PM/HK P2 risk/portfolio/replay 工具已本地模块级实现；6 styles × 4 markets × 5min 的 JSON 驱动多风格 simulated 已扩展为绩效追踪、权重调节、paused/deprecated 状态和 variant 生成闭环，并新增 evolution guard 防止全风格亏损、组合回撤和连续多市场亏损时继续自演化；新增 `shared/execution/auto_pipeline.py` 将 universe、研究、DecisionEngine、StyleRunner 和 daily evolution 串成 simulated 自动管线；本地 production sim 层已补齐 `sim_engine`、`risk_manager`、`sim_ledger` 并接入 auto pipeline，仍需生产调度和报告闭环验证
- **实盘安全基础设施**：新增 `shared/execution/real_trading_gate.py` 与 `signals_real.py`，真实交易默认拒绝，必须显式环境开关、人工确认 token、资金上限、交易时段、T+1 与 halt 检查全部通过；sim → real promotion 只接受经 sim 审计的来源；`signals/real/*` 为隔离队列，不代表自动下单或已成交
- **cron 解耦入口**：Crypto/US/PM/HK 兼容入口已改跑 `job_*_sim_exec` 多风格 simulated；旧 cron 已退役（0 enabled）；`job_style_evolution` 模板每 4 小时跑 simulated 演化；`cron/daily_review.sh` 16:00 做复盘与演化摘要；新增 `cron/health_check.sh` 上报 SharedSignals/TradingAgent/MarketGraph 统一健康；新增 `cron/auto_pipeline.sh` 模板按工作日 09:00 运行 simulated 自动管线；均带 flock 与独立日志
- **SharedSignals API 消费**：`SharedSignalsAPIClient` 已校准 15/15 数据端点；`TradingagentDataReader` 已对核心读取路径启用 API-first，SQLite 只读回退保留
- **研究/筛选增强**：新增 `shared/screening/fundamental_analyzer.py` 和 `shared/research/multi_perspective.py`，只读消费 SharedSignals API/DB，输出基本面质量分、同业比较、red flags 和 bull/bear/macro/technical 多视角共识报告；`auto_pipeline` 消费这些研究结果生成 simulated 决策，不触碰实盘队列
- **复盘节奏**：11:45 午盘 / 15:30 收盘 / 22:00 夜间校准 / 07:30 晨报
- **服务端**：杭州 `8.138.181.177`，生产路径 `/opt/investment/tradingagent/`
- **运行监控**：每小时运维报告（`ops_report.py`），覆盖执行队列、sim 队列、回执完整性、PnL 摘要
- **邮件模板**：11 类 TradingAgent 邮件已统一为移动端 30 秒决策版，顶部决策条、交易执行边界、三张摘要卡和日报/周报 inline SVG 图表已补齐；通道映射未变

## 二、已知问题

- Crypto/US/HK/PM 模拟盘完整生产闭环未验证（仅 A 股链路完整；多市场 P0/P1/P2 工具和 production sim layer 为本地模块级验证）
- 多市场旧系统 symlink 依赖已全部清除（61 个死 symlink）；工具独立实现已完成，剩余风险在生产调度、账本和日报闭环
- 集合竞价支持标记为 STUB，未实现
- A 股实盘路径仍是人工；当前只补齐本地 fail-closed 安全门和 `signals/real/*` 隔离队列，未部署为自动下单路径

## 三、下一步

1. [x] **P2：Crypto/US/PM/HK 多市场工具独立实现** — Crypto risk/portfolio/replay、US portfolio/replay、PM risk、HK portfolio 已补齐
2. [ ] **P2：多市场模拟盘生产闭环** — 工具已独立，多风格 simulated cron/evolution 入口、evolution guard、auto pipeline、production sim engine/risk/ledger 已补齐；仍需验证生产调度、报告、promotion、权重演化、guard halt/thaw 和失败回退闭环
3. [ ] **P2：A 股实盘路径设计** — 需先确认安全边界和人工确认环节
4. [x] **P2：SharedSignals HTTP API 消费迁移** — 15/15 端点客户端已完成；`TradingagentDataReader` 已对 `get_market_data` / `get_events` / `is_trading_day` 接入 API-first 访问；SQLite 只读回退保留

## 四、活跃任务

（当前无活跃迁移任务）

## 五、最近完成

### 2026-07-04 六维打分异常处理修复

- [x] `shared/screening/six_dimension_scorer.py` 的 6 个 `_score_*` 维度函数已改为：数据缺失仍返回中性缺省，真实异常记录 `logger.error(..., exc_info=True)` 并返回 `None`。
- [x] `score_stock()` 统一处理 `None` 为 `combined.missing_default`，避免维度函数把异常静默伪装成有效 0.5 分。
- [x] 验证：`py_compile shared/screening/six_dimension_scorer.py` 通过；本地 smoke 确认异常维度返回 `None`，外层按缺失数据回退。

### 2026-07-04 shadow broker + evolution guard/cron 修复

- [x] 新增 `shared/execution/shadow_broker.py`：影子盘只记录不执行，支持 JSONL trade log、JSON positions/pnl、按 strategy/date/market 查询 PnL，并拒绝 real/live/direct payload。
- [x] `shared/markets/evolution_engine.py` 的 `evaluate_all_markets()` 已前置调用 `evaluate_guard()`；guard 阻断时返回 `state=guard_blocked`，不执行调权或 variant 生成。
- [x] `cron/evolution.sh` 已改为调用函数入口 `evaluate_all_markets()`，保留 flock、日志、timeout、`.env` 读取和可配置 markets/review root。
- [x] 验证：`py_compile`、`bash -n cron/evolution.sh`、shadow/router 聚焦回归 20 项 + 4 subtests、orchestrator/sim loop 回归 9 项 + 2 subtests、evolution guard smoke 通过。

### 2026-07-04 多市场 shadow runner 补齐

- [x] 新增 `PM/shadow_runner.py`、`Crypto/shadow_runner.py`、`US/shadow_runner.py`、`HK/shadow_runner.py`，均为 sim/shadow-only：读取 universe、生成最小 mock order、调用本地 simulator、写入 `shadow/pending` signal card。
- [x] 写入卡片固定 `capital_layer=shadow`、`account_type=shadow`、`real_execution=false`、`direct_execution=false`，并复用现有安全 guard 拒绝 real/live/direct payload。
- [x] 验证：新增文件 `py_compile` 通过；`tests/test_market_base_layer.py tests/test_pm_workflow_config.py tests/test_crypto_phase_d_tools.py tests/test_us_hk_phase_d_p0.py` 通过（15 passed，7 subtests passed）。

### 2026-07-04 production-grade simulated execution layer

- [x] 新增 `shared/execution/sim_engine.py`：`SimOrder` / `SimFill` / `SimPosition`、A-share 1bps commission + 2.5bps sell stamp duty、波动率滑点、partial fill、queue position、price improvement 和 `pending -> open -> partial/filled/cancelled/rejected` 状态机。
- [x] 新增 `shared/execution/risk_manager.py`：JSON profile 驱动的 max order/position/notional/daily loss、global/market/symbol/hardware halt、volatility/loss circuit breaker、gross/net/delta/beta exposure 和 symbol/sector/market concentration 检查。
- [x] 新增 `shared/accounting/sim_ledger.py`：append-only trade journal、FIFO position/tax lots、cash ledger、double-entry records、daily mark-to-market、CSV/JSON audit export。
- [x] `auto_pipeline` 的 `execute_sim` 阶段已先走 production sim risk -> engine -> ledger，再保留原 StyleRunner 风格比较输出；所有输出继续固定 `capital_layer=simulated`、`real_execution=false`。
- [x] 每个迁移对象提供 `to_real()` placeholder，默认受 `REAL_TRADING_ENABLED` fail-closed 保护，不会自动变成真实交易入口。
- [x] 验证：新增 `tests/test_sim_production.py` 6 项通过；相关回归 `tests/test_sim_production.py tests/test_auto_pipeline.py tests/test_sim_broker_v2.py tests/test_local_sim_ledger.py tests/test_multi_style_sim.py` 共 16 项通过；新增/修改 Python `py_compile` 通过。

### 2026-07-04 simulated auto pipeline

- [x] 新增 `shared/execution/decision_engine.py`：将基本面质量分、多视角 consensus、red flags 合成为 buy/watch/skip 决策，并通过组合构建输出 simulated target positions。
- [x] 新增 `shared/execution/auto_pipeline.py`：按 Crypto/US/PM/Ashare 执行 pre-market scan、research、decision、sim execution 和 daily review/evolution；候选、决策、账户和信号均强制 `capital_layer=simulated`、`real_execution=false`，发现 real/live 负载直接跳过或拒绝。
- [x] 新增 `cron/auto_pipeline.sh` 与 `shared/crontab.txt` 模板行：`0 9 * * 1-5 /opt/investment/tradingagent/cron/auto_pipeline.sh`；脚本带 flock、日志和 timeout。
- [x] 验证：`py_compile` 通过；`bash -n cron/auto_pipeline.sh` 通过；`tests/test_auto_pipeline.py` 3 项通过；auto pipeline + multi-style/evolution/fundamental/multi-perspective 相关回归 12 项通过。
- [ ] 待服务器部署验证：生产 crontab 尚未安装 `cron/auto_pipeline.sh`；Ashare 当前通过本地 simulated adapter 保持不触碰 Hermes/Mac Mini/同花顺。

### 2026-07-04 主动基本面分析 + 多视角研究报告

- [x] 新增 `shared/screening/fundamental_analyzer.py`：通过 SharedSignals API/DB 读取 income、balancesheet、cashflow、fina_indicator、daily_basic、industry/reference，计算 ROE、ROA、debt/equity、current ratio、gross margin trend、revenue growth YoY、FCF yield、PE/PB 5 年分位和 PEG。
- [x] 基本面报告输出 0-100 composite quality score、SW L3 行业同业比较、估值分位、数据覆盖和 red flags；同业样本不足或财报缺失时降级记录，不伪造结论。
- [x] 新增 `shared/research/multi_perspective.py`：bull、bear、macro、technical 四视角分别打分并提供 evidence，综合 weighted consensus、conviction level 和 disagreement areas。
- [x] `TradingagentDataReader.get_tushare()` 补充额外参数透传，支持 `daily_basic`/财务接口扩展读取；仍为只读 API-first，失败返回空列表。
- [x] 验证：新增/修改 Python `py_compile` 通过；`tests/test_fundamental_analyzer.py`、`tests/test_multi_perspective.py` 3 项通过；`tests/test_data_reader.py` 5 项通过。

### 2026-07-04 simulated evolution guard + 跨系统健康上报

- [x] 新增 `shared/markets/evolution_guard.py`：全风格当日亏损暂停 evolution、组合回撤 -20% 冻结权重、恢复时 thaw，连续 3 天所有市场亏损时写 `shared/review/SIM_HALT.json` 并可发送紧急告警。
- [x] `evaluate_all_markets()` 接入 guard；多市场自动演化任务在 guard 阻断时返回 `state=guard_blocked`，不生成新 variant、不调权。
- [x] 新增 `cron/health_check.sh`：检查 SharedSignals API、TradingAgent `style_comparison.json` 新鲜度、MarketGraph 数据新鲜度，并写入 SharedSignals `logs/watchdog_inputs/tradingagent_health.json` 供 watchdog 汇总。
- [x] 验证：`py_compile` 通过；`bash -n cron/health_check.sh` 通过；`tests/test_evolution.py` 3 项通过。
- [ ] 待服务器部署验证：生产 crontab 尚未安装 `cron/health_check.sh`，guard circuit breaker 仅完成本地语法和既有演化回归验证。

### 2026-07-04 多市场多风格 simulated 自演化闭环

- [x] 新增 `shared/markets/performance_tracker.py`：`StylePerformance`、`style_performance.jsonl` 追加写入、90 天历史加载、PnL 趋势回归和风格综合排序。
- [x] 新增 `shared/markets/evolution_engine.py`：按风格综合分、趋势和连续亏损天数执行 promote/demote/deprecated，并能从优胜风格生成下一代 variant。
- [x] `StyleRunner` 接入 evolved `style_weights.json`，active 风格按权重切分 simulated capital，paused/deprecated 不参与执行；每轮 style metrics 自动写绩效 JSONL。
- [x] 各市场 `styles/*.json` 补齐 `status`、`weight`、`created_at`、`last_modified`、`generation` 和 `auto_generate` 参数范围；HK 继续 paused。
- [x] `daily_review` 收盘复盘接入演化摘要，显著变化时使用 `strategy_invalidation` 模板发送 simulated 策略调整通知。
- [x] 新增 `job_style_evolution.sh` 与 crontab 模板：演化每 4 小时运行，daily review 16:00 运行；尚未声明生产 crontab 已安装。
- [x] 新增 `tests/test_evolution.py` 覆盖绩效追踪、权重执行、调权、废弃和变体生成。

### 2026-07-04 多市场多风格 simulated 比较层

- [x] 新增 `shared/markets/style_config.py` / `style_runner.py`：`TradeStyle` dataclass、JSON 风格加载、每个信号并行套用 enabled styles、输出 `style_comparison` 矩阵。
- [x] Crypto/PM/US/HK 各新增 6 个 `styles/*.json`；Crypto/PM/US 默认启用，HK 默认暂停（`enabled=false`）。
- [x] 四个市场 simulator 新增 `run_style_simulation()`，workflow 新增 `run_*_sim_cycle()`；旧兼容入口已退役为 sim-only 路径。
- [x] cron/wrapper 新增 `job_crypto_sim_exec`、`job_pm_sim_exec`、`job_us_sim_exec`、`job_hk_sim_exec`；旧兼容入口改跑 multi-style simulated，旧 cron 当前为 0 enabled。
- [x] 所有 style run 固定 `capital_layer=simulated`、`account_type=simulated`、`real_execution=false`，不触碰 `signals/real`。

### 2026-07-03 TradingAgent 邮件模板移动端决策版

- [x] 11 个邮件模板全部改为移动优先决策结构：顶部 `ACT/WAIT/IGNORE` 决策条、三张摘要卡、暗色 header、白色卡片和 HTML5 section/article/figure 语义结构。
- [x] 交易类模板补齐执行边界字段：market、capital_layer、route、signal_time、expires_at、data_fresh_at、broker_status、receipt_status；系统类模板不展示交易执行边界，避免误读为下单指令。
- [x] 日报和周报新增 3 类纯 inline SVG 图表：PnL sparkline、策略贡献横条、持仓热力图。
- [x] `system_health` / `emergency_alert` 将技术状态转为面向交易判断的自然语言，例如“交易信号管道异常，当前信号不可信”和“数据校验通过，信号质量正常”。
- [x] 验证：11 个模板 `py_compile` 通过，最小 render smoke 确认每个模板 1 个决策条和 3 张摘要卡，日报/周报各 3 个 SVG；TradingAgent 全量测试通过（214 passed，17 subtests passed）。

### 2026-07-03 R24 real signal promotion source guard

- [x] `shared/execution/signals_real.py`：旧 promotion guard 新增来源路径校验，只接受经 sim 审计的信号来源，非 sim 或缺失来源路径均 fail-closed。
- [x] 实盘 review card 保留 sim 来源路径，便于后续审计 promotion 来源。
- [x] 新增回归测试覆盖非 sim 来源拒绝，并更新合法 promotion 用例带上 sim 来源路径。
- [x] 验证：`tests/test_real_trading_gate.py tests/test_real_money_boundary.py` 17 项通过；全量 `python3 -m pytest tests/ -q --tb=line` 通过（214 passed，17 subtests passed）。

### 2026-07-03 实盘安全基础设施（Phase B）

- [x] 新增 `shared/execution/real_trading_gate.py`：`REAL_TRADING_ENABLED` 默认拒绝，显式人工 token、单笔/单日资金上限、A股交易时段、T+1 与 emergency halt 任一失败均抛 `SafetyViolation`。
- [x] 新增 `shared/execution/signals_real.py`：`RealSignalQueue` 使用 `signals/real/*` 隔离队列，promotion、manual confirm、pending submit、签名回执和持仓对账均不触碰 sim 队列。
- [x] 更新 `shared/execution/__init__.py` 导出实盘安全门和真实队列入口。
- [x] 更新 `AGENTS.md` 固化实盘安全门边界：`signals/real/pending` 不代表自动下单或已成交，回执必须带 checksum。
- [x] 验证：新增 `tests/test_real_trading_gate.py` 11 项；执行层相关回归 30 项通过；全量 `python3 -m pytest tests/ -q --tb=line` 通过（213 passed，17 subtests passed）；新增/修改 Python 文件 `py_compile` 通过。

### 2026-07-03 cron 解耦入口补齐

- [x] 新增多市场 cron 兼容入口，当前已改跑 `job_*_sim_exec`。
- [x] HK 兼容入口已改跑 sim cycle，只写 sim 输出。
- [x] 新增 `cron/daily_review.sh`：调用 `shared.review.daily_review.run_daily_review()`，按 sim 日志汇总多市场 review。
- [x] 新增 `cron/AGENTS.md`：约束 cron wrapper 不内嵌 broker 凭据、实盘 payload 或审批捷径。
- [ ] 待服务器部署验证：脚本尚未写入生产 crontab；多市场生产闭环仍需实跑确认。

### SharedSignals API 15/15 端点迁移对齐（2026-07-03）

- [x] `SharedSignalsAPIClient` 已覆盖 15 个数据端点：trading day、market data、fundamentals、reference、macro、capital flow、events、sentiment、crypto、PM、associations、impacts、industry、realtime 5min、tushare。
- [x] `TradingagentDataReader` 已接入 API-first 访问核心读取路径；API 不可用时回退 SQLite 只读路径并打 degraded 状态。
- [ ] MarketGraph 侧仍待迁移；TradingAgent 侧不再把 15/15 客户端视为孤儿代码。

### 多市场 P2 工具本地实现（2026-07-03）

- [x] `Crypto/risk.py`：新增 `CryptoRiskBackground`，基于 funding/news/volatility 的公开数据风险评分。
- [x] `Crypto/portfolio.py`：新增 `CryptoPortfolioOptimizer`，输出相关矩阵与波动率自适应 sim 权重。
- [x] `Crypto/replay.py`：新增 `CryptoHistoricalReplay`，用公开历史 bars 回放 sim 规则。
- [x] `US/portfolio.py` / `US/replay.py`：新增美股相关性持仓门与历史 bars 回放。
- [x] `PM/risk.py`：新增 `PMRiskControl`，执行单市场 5% 上限与相关 topic 上限。
- [x] `HK/portfolio.py`：新增 HKD lot sizing 与 sector cap 组合工具。
- [x] 所有 P2 工具保持 sim only，拒绝 real/live/direct execution 负载或配置。
- [x] 验证：新增 `tests/test_multi_market_p2_tools.py` 14 项通过；P0/P1/P2 关联测试 39 项通过；新增文件 `py_compile` 通过。

### final Codex review HIGH/MEDIUM 安全修复（2026-07-03）

- [x] `shared/markets/safety.py`：`reject_real_execution_payload()` 改为递归扫描嵌套 dict/list，并拒绝 `direct_execution=True`、`real_execution=True`、`live=True` 等真实执行别名。
- [x] `US/simulator.py`、`HK/simulator.py`：`simulate()` 对 order 和 account 同时执行真实执行拒绝；fill 结果固定 `capital_layer=simulated`、`account_type=simulated`，不再回显 account payload。
- [x] 新增回归测试覆盖嵌套真实执行负载、US/HK simulator order/account 拒绝和 account 不回显。
- [x] 验证：`python3 -m pytest tests/ -q --tb=line` 通过（188 passed，17 subtests passed）。

### 多市场 promotion tier 命名统一（2026-07-02）

- [x] `Crypto/promotion.py`、`PM/promotion.py` 晋级口径统一为 `research -> sim_candidate -> sim`，与当前多风格模拟盘口径一致。
- [x] Crypto/PM `eligible_for_sim` 与 `target_layer=simulated` 改为只在 `tier=sim` 时成立。
- [x] 更新 Crypto/PM P1 测试断言，覆盖统一五档 tier 名。
- [x] 验证：`tests/test_crypto_p1_tools.py`、`tests/test_pm_p1_tools.py`、`tests/test_us_hk_p1_tools.py` 共 17 项通过；完整 `python3 -m pytest tests/ -q --tb=line` 187 项通过。

### SharedSignals HTTP API 消费迁移（2026-07-02）

- [x] `TradingagentDataReader` 新增 `api_client` 参数；配置 `SHAREDSIGNALS_API_URL` 时自动创建 `SharedSignalsAPIClient`。
- [x] `get_market_data` / `get_events` / `is_trading_day` 优先走 SharedSignals HTTP API；API 不可用时回退 SQLite 只读路径并设置 `degraded=True`。
- [x] `SharedSignalsAPIClient` 移除 deprecated 状态，校准 15 个当前 API server 端点，补充 timeout / retry / backoff 配置，去除 `X-API-Key` 双重暴露。
- [x] `.env.example` 新增 `SHAREDSIGNALS_API_URL`（默认空，直接走 SQLite）、`SHAREDSIGNALS_API_KEY`、timeout/retry 配置。
- [x] 验证：`py_compile`、导入 smoke、`tests/test_data_reader.py` 通过。

### 多市场 P1 Codex review 修复（2026-07-02）

- [x] `PM/report.py`：`_outcome()` 改为显式 outcome 白名单，`cancelled`、`void`、`pending`、`unresolved` 等未知/未决状态不再计为 resolved YES。
- [x] `Crypto/validation.py`、`PM/validation.py`：OOS 日期比较前统一规整为 `YYYYMMDD`，兼容 `2026-07-02`、`20260702`、`2026-7-2`。
- [x] `US/validation.py`、`HK/validation.py`：补齐 `train_end` OOS 过滤，排除训练期及晚于 `as_of` 的记录；`_to_float()` 增加 NaN 防护。
- [x] 测试覆盖 PM 未决 outcome、Crypto/PM 日期格式规整、US/HK OOS 过滤和 NaN 防护；指定 P1 pytest 与变更文件 `py_compile` 通过。

### Crypto/PM P1 工具（2026-07-02）

- [x] `Crypto/report.py`：新增 `CryptoDailyReport(BaseReport)`，生成每日 sim 复盘并执行 no-empty-trigger 规则，未触发时返回 no-send。
- [x] `Crypto/validation.py`：新增 `CryptoForwardValidation`，计算 OOS win rate、PnL、direction hit rate 与样本质量评分。
- [x] `Crypto/promotion.py`：新增 `CryptoStrategyPromotion`，提供 sim 晋级门。
- [x] `PM/report.py`：新增 `PMDailyReport(BaseReport)`，生成每日 Brier + PnL sim 报告。
- [x] `PM/validation.py`：新增 `PMForwardValidation`，计算 OOS Brier、PnL 与校准分箱。
- [x] `PM/promotion.py`：新增 `PMStrategyPromotion`，提供 research→sim 晋级门。
- [x] 所有 P1 工具保持 sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_crypto_p1_tools.py` 与 `tests/test_pm_p1_tools.py`，Crypto/PM 各 4 项测试；`py_compile`、`compileall`、Crypto/PM P0 回归测试通过。

### US/HK P1 工具（2026-07-02）

- [x] `US/report.py` / `HK/report.py`：新增 Markdown 日度 sim 报告；HK 报告包含 lot size；默认只渲染不发送。
- [x] `US/validation.py` / `HK/validation.py`：新增 OOS 前向验证；US 覆盖 earnings/momentum funnel，HK 使用 HKD 口径。
- [x] `US/promotion.py` / `HK/promotion.py`：新增 `research -> sim_candidate -> sim` 策略晋级分类。
- [x] 所有 P1 工具维持 sim 边界，拒绝 real/live/direct execution 配置或负载。
- [x] 新增 `tests/test_us_hk_p1_tools.py`，US/HK 各 3 项测试；`py_compile`、US/HK P0 回归和 diff 检查通过。

### R8/R9 多市场安全修复（2026-07-02）

- [x] `PM/config.yaml` currency 从 USD 修正为 USDC，并补充 `PMWorkflow()` 读取 checked-in config 的回归测试。
- [x] PM 旧 runner 已纳入 sim-only 状态机写入路径。
- [x] `shared/markets/safety.py`、`base_tools.py`、`config_schema.py` 增加真实执行/live broker/direct execution 拒绝；`execute_sim_order()` 不再把真实负载静默改写成 simulated 后派发。
- [x] `PM/simulator.py` 拒绝 real order，fill 结果固定返回 `capital_layer=simulated`、`account_type=simulated`。
- [x] `PM/Crypto/US` sim executor 入口新增真实执行负载拒绝；修复 `US/sim_executor.py` 删除 account 后再访问的 `UnboundLocalError`。
- [x] `HK/workflow.py` 补齐 `HKWorkflow` / sim cycle，对齐 US workflow 模式。
- [x] 新增/更新测试覆盖 PM config load、HK workflow smoke、US/HK live broker rejection、sim executor safety。

### Mini/服务器执行桥路径修复（2026-07-02）

- [x] 禁用旧 `ai.hermes.sim-remote-sync`，停止 `~/Desktop/Investment/Ashare/outputs/account` 被周期性重建。
- [x] 禁用旧 `ai.hermes.condition-cleanup`，停止访问已退役的 `~/Desktop/Investment` tradebook 清理路径。
- [x] Mac Mini executor 明确设置 `SIM_REMOTE_TRADINGS_SIGNAL_DIR=/opt/investment/tradingagent/signals`。
- [x] 服务器合并 GitHub 最新 main，修复 `TradingagentDataReader` 导入/导出和 reader 回归；`tests/test_data_reader.py` 通过。
- [x] `shared/execution/execution_router.py` 已提交：A 股 sim broker 正式通过仓库内 `Ashare/sim_executor.py`，不再依赖旧 `/opt/investment/Ashare/tools`。
- [x] Mac Mini live executor / receiver / health-check 默认路径已改到 `~/.hermes/ashare-runtime` 与 `/opt/investment/tradingagent/signals`。
- [x] 清理 `mini/README.md`、`mini/mini_consumer.py`、`Ashare/AGENTS.md` 等参考副本里的误导性旧执行器路径。
- [x] 归档 Mac Mini 非活跃旧脚本/备份/禁用 LaunchAgent；active scripts、LaunchAgents、crontab 旧路径审计为 0 命中。
- [x] 记录事件日志：[docs/runtime_incidents_20260702.md](docs/runtime_incidents_20260702.md)。

**残余风险：**
- 未在交易时段发送测试交易信号；完整端到端验证需等交易时段。

### Goal 2 审计 — SharedSignals → TradingAgent → MarketGraph 数据流

**2 轮审计，10 维度，46 发现，10 项修复全部完成。**

**Round 1（5 agents，17 发现）：**
- API 客户端：`is_trading_day()` 默认返回 False（fail-safe）、API sentinel→TTL 恢复、4xx/5xx 重试分化
- 健康检查：sockstat 端口检测替代 HTTP health check（30s SIGALRM 超时）
- 配置一致性：端口 8082/8900 不一致修复、MarketGraph `.env.example` port 更新

**Round 2（5 agents，29 发现，5 维度）：**
- 数据新鲜度：LRU cache 无 TTL、naive `datetime.now()`（20+ 处）、无每日管线 fallback cron
- 错误传播：dead `api` property、`errors`/`stale` 从未消费、SQLite 错误静默吞掉、无死人手刹
- 配置漂移：`SHAREDSIGNALS_ROOT` 指向错误、端口 8900/8082 不一致、`MARKETGRAPH_ENV_FILE` 路径冲突、15 个未文档化环境变量
- MarketGraph 直接读取器：从未使用 HTTP API、reference/ 下断 symlink、直接导入无鉴权
- 密钥暴露：`api_tokens.json` 在 git 中追踪、无盐 SHA256、X-API-Key 双重暴露、`.env.*` 不在 gitignore

**已应用修复（10 项）：**
1. [x] `SharedSignals/.gitignore`：添加 `config/api_tokens.json` + `.env.*`
2. [x] `.env.example`：`SHAREDSIGNALS_ROOT` 修正（MarketGraphRuntime → SharedSignals）
3. [x] `.env.example`：`MARKETGRAPH_ENV_FILE` 修正（MarketGraph/.env → marketgraph/.env）
4. [x] `SharedSignals/tools/api_server.py`：端口默认值 8900 → 8082（docstring + env.get）
5. [x] `shared_signals_api.py`：移除 X-API-Key 双重暴露（服务器仅检查 Authorization）
6. [x] `reader.py`（TradingagentDataReader）：移除 dead `api` property + 未使用的 `import time`
7. [x] `SharedSignals/auth.py`：添加 salt token hashing（PBKDF2-HMAC-SHA256，100k 迭代，向后兼容）
8. [x] `SharedSignals/reader.py`：LRU cache 失效 — 14 个缓存函数已注册，TTL（默认 5 分钟）+ 文件 mtime 自动检测，`clear_caches()` + `/cache/invalidate` + `/cache/status` 端点

### Goal 2 审计 Round 3（高强度终检 — TradingAgent 侧）

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **MarketGraphCSVReader 路径错误：** `intake` 路径缺少 `data/` 目录，`get_regime()` 路径错误 — 导致体制信号、事件候选、情绪信号三个关键 CSV 静默加载失败（已修复）
- **SharedSignalsAPIClient 孤儿代码：** `shared_signals_api.py` 已定义完整 HTTP 客户端（15 接口），但 `TradingagentDataReader` 从未实例化或使用它 — 所有数据仍走直接 SQLite 读取
- **TradingagentDataReader 无数据新鲜度检查：** `errors`/`stale` 字段只写从未被消费，stale=True 后无任何恢复逻辑
- **N+1 查询扇出：** 评分管线对每只股票做 5-6 次独立查询，20 只股票 > 100 次调用，无批量接口
- **直接 SQLite 读取绕过了 API 鉴权：** TradingAgent 绕开 SharedSignals HTTP API 直接读 SQLite，使得 API token/scope 安全模型形同虚设
- **无死人手刹：** 连续 N 次 SQLite 错误或 CSV 空返回后无告警

**已应用修复（Round 3，影响 TradingAgent）：**
9. [x] `tradingagent/shared/data/reader.py`：MarketGraphCSVReader `intake` 路径从 `self.root / "intake"` → `self.root / "data" / "intake"`，`get_regime()` 路径从 `self.root / "all_weather_regime.csv"` → `self.root / "data" / "all_weather_regime.csv"`

### Goal 2 审计 Round 4（终检 — TradingAgent 侧）

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **SharedSignalsAPIClient 孤儿代码：** 214 行 HTTP 客户端从未被生产代码导入使用（已加 DeprecationWarning）
- **TradingagentDataReader 无死人手刹：** `errors` 列表无限增长但从不消费，`stale` 标志从未检查 — 已修复：添加 `_maybe_alert()` 每 10 条错误 WARNING
- **SharedSignalsReader 无 SQLite busy_timeout：** 连接无超时，写锁期间读立即失败 — 已修复：添加 `busy_timeout = 5000`
- **静默回退到 `/dev/null/does_not_exist.sqlite`：** 初始化失败时所有查询返回空，零告警 — 已修复：`_maybe_alert()` 在回退激活时日志记录
- **重复的交易日历实现：** `t_plus_1.py` 和 `position_schema.py` 各自独立实现 is_trading_day，行为不一致
- **try/except 将类设为 None 反模式：** `daily_review.py` 和 `benchmark.py` 将导入失败转换为 None，隐藏真实错误
- **N+1 查询扇出：** 评分管线每只股票 5-6 次独立查询，无批量接口
- **数据类型不一致：** CSV 路径返回字符串，SQLite 路径返回正确 Python 类型

**已应用修复（Round 4，影响 TradingAgent）：**
10. [x] `reader.py`：SharedSignalsReader 连接添加 `PRAGMA busy_timeout = 5000`
11. [x] `reader.py`：TradingagentDataReader 添加 `_maybe_alert()` — errors 每 10 条 WARNING，所有 9 个 error.append 点均已接线
12. [x] `shared_signals_api.py`：添加 DeprecationWarning 模块级警告
13. [x] `SharedSignals/reader.py`：`get_market_data()` 查询添加 `market` 过滤（从 ts_code 后缀推导）
14. [x] `SharedSignals/collectors/rss/`：event_hash 从 64 位升级到 128 位（collector.py + gap_filler.py）
15. [x] `SharedSignals/api_server.py`：恢复 `log_message()` HTTP 请求日志 + 500 错误日志

### 2026-07-02 Goal 2 审计 Round 5（五维度最终审计 — 58 发现，23 修复）

**5 新维度并行审计。TradingAgent 相关发现和修复：**

**TradingAgent 相关发现（CRITICAL/HIGH）：**
- **Universe collapse（CRITICAL）：** `adapter.py:_exclude_asset()` 将 `None`（DB 错误）等价于"低流动性" — 所有股票被排除，TradingAgent 生成零交易
- **SQLite 读写模式（HIGH）：** `reader.py` 以 rw 模式打开 SharedSignals 只读模型 DB — 损坏风险，空 DB 静默创建
- **`/dev/null` fallback（HIGH）：** `SharedSignalsReader` 初始化失败静默回退到 `/dev/null/does_not_exist.sqlite` — 完全数据丢失零告警
- **DatabaseError 未捕获（HIGH）：** `_query()` 只捕获 `sqlite3.OperationalError` — DB 损坏绕过防御
- **硬编码 secrets（HIGH）：** `webhook_sender.py` 中 WEBHOOK_SECRET 和 WEBHOOK_URL 为硬编码字面量
- **env 自动加载在 import-time（MEDIUM）：** 多个模块在 import 时 mutate `os.environ`

**已应用修复（Round 5，5 TradingAgent 相关项）：**
1. [x] `Ashare/adapter.py`：`_exclude_asset()` — `amount is None` 返回 False（保留），记录 WARNING
2. [x] `shared/data/reader.py`：SQLite 连接从 rw 改为 `mode=ro` URI
3. [x] `shared/data/reader.py`：`DatabaseError` 和 `OperationalError` 同时捕获
4. [x] `shared/data/reader.py`：`/dev/null` fallback → RuntimeError（fail-fast）
5. [x] `shared/execution/webhook_sender.py`：Hardcoded secrets → env vars

### Goal 1 退役清理

- [x] Ashare 依赖迁移：`execution_router.py` sim_broker 通道从旧 `/opt/investment/Ashare/tools/a_share_simulated_trade_executor` 迁移到 `tradingagent/Ashare/sim_executor.py`
- [x] Tushare API 包装器迁移：`a_share_tushare_api.py` + `a_share_common.py` 已迁至 `/opt/investment/SharedSignals/collectors/tushare/`，服务器保留兼容性 symlink
- [x] Ashare/tools 全面退役：142 文件归档至 `_archive/Ashare_tools_20260702/`，目录仅剩 3 个 compat symlink
- [x] `Ashare/AGENTS.md` 添加迁移注释
- [x] 旧系统残留清理：删除 61 个死 symlink（PM 20 + Crypto 21 + US 20）
- [x] 代码层 Tradings/KimiWork 引用全部修复（0 残留）
- [x] 服务器 crontab 37 条旧注释清理
- [x] 所有修改提交并 push，服务器同步确认
- [x] `sim_broker.py` L8 + `slippage_model.py` L8 注释引用路径更新至 `_archive/Ashare_tools_20260702/`

## 六、7/1 事故复盘（已完成）

以下事项已修复并固化为 [AGENTS.md](AGENTS.md) 中的永久规则：

- 虚假成交确认 → Mini/Hermes 健康门 + 未确认回执 halt
- 过期 pending 清理 → job_self_heal
- 回执指纹闭环 → receipt_sha256 验证
- sim 信号过滤 → 200xxx.SZ 等非普通 A 股代码三层过滤

详细时间线：[docs/runtime_incidents_20260701.md](docs/runtime_incidents_20260701.md)

## 六、关联系统状态

- [SharedSignals STATUS](../SharedSignals/STATUS.md) — 数据采集与存储状态
- [MarketGraph STATUS](../MarketGraph/STATUS.md) — 研究图谱与因果状态
- [Finance STATUS](../STATUS.md) — 根工作区总览
