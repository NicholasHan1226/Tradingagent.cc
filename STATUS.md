# TradingAgent 状态

> **给所有 agent：** 读完 [AGENTS.md](AGENTS.md) 理解规则后，读本文件理解"现在在哪、要去哪、能做什么"。
>
> **⚠️ 变更后必须更新本文件。**
>
> 最后更新：2026-07-08 (A股 candidate 研究证据门禁、盘前验收候选池口径统一)

---

## 一、当前状态

- **A 股多风格模拟盘**：完整闭环运行（信号生成 → server-local paper fill → sim 账簿 → 复盘）；旧层已完全退役（0 文件、0 cron）；A股资产入口已通过 SharedSignals `/tushare?api_name=stock_basic` 恢复；2026-07-06 已修复候选池/执行门禁退化问题：auto pipeline 的 A股入口改走 `AshareAdapter` 过滤后的 universe 与真实分钟/日线价格，不再从资产表顺序取样或使用 `price=1.0`，`run_sim_loop` 的 A股 simulated 新买入只允许 candidate 层，watch/空池/candidate pool 异常均 fail-closed 为无交易，缺名称/缺日线/缺流动性证据标的不进入可执行候选；A股 simulated 订单必须写入 `candidate_pool_layer` 与 `execution_source`，`sim_broker` 与本地模拟账本双层拒绝缺来源买入/卖出，signal card 会持久化同一来源字段，复盘可直接确认买入来自 candidate 层或卖出来自 rebalance；缺少来源字段的历史 A股 simulated 成交只保留在隔离备份中作事故复盘，不进入当前模拟账户、策略有效胜率、方向命中、归因、策略 PnL 或自我进化；2026-07-07 起 A股评分覆盖扩到 500 个 universe 样本，候选池仍坚持 `combined >= 0.55` 的 candidate 门禁，若候选为 0，会在 `no_trade_explanation.score_diagnostics` 写出已评分数量、阈值、Top 分数、各维度 0.5 中性默认计数、缺失计数、全维度 0.5 样本数和样本列表，并新增 `evidence_reason_summary`、`missing_and_default_like_dimension_counts`、`evidence_coverage_distribution` 和 reason sample；2026-07-08 起 A股 candidate 额外要求证据元数据支持：最低 evidence coverage 且 event/fundamental/sentiment 至少一个研究维度有真实证据，避免纯技术/资金高分叠加缺失维度 0.5 中性默认分进入可执行候选；不满足证据门禁但分数较高的标的留在 watch。盘前 dry-run 已改为复用 `candidate_pool.build_pool`，与模拟主循环、开盘验收共用同一候选池口径；已传入预计算 scores 的 5 分钟/盘前高频入口默认跳过低频 fundamental 全量观察池，避免候选池验收被长期基本面池拖慢；诊断会区分“分数过阈值但实际 candidate 为空”的分层/证据门禁拦截。
- **A 股模拟盘**：默认走服务器本地闭环，不依赖 Mac Mini Hermes；Hermes/同花顺 GUI 路径已降级为第二选择，只在 `ASHARE_SIM_HERMES_ENABLED=1` 时启用并投递 `signals/pending`；A股 simulated signal card 显式固定 `real_trading_enabled=false`；2026-07-05 已修复 A股 sim account 字符串阻断 server-local fill 的问题，隔离真实数据 smoke 验证 9/9 本地成交、local_sim 账本与 `signals/positions/simulated_ashare_positions.json` 持仓快照均可生成；A股 simulated capital 已显式固定为 200,000 元，`job_ashare_sim_exec` 会在开盘前/盘中首轮保证空账本快照存在，尚无成交时输出 `bootstrap_state=no_trades_yet`、现金与空持仓，避免 dashboard/验收等待第一笔成交；2026-07-06 已统一 A股 server-local 执行器和本地模拟账本默认资金为 200,000 元，本地账本会从成交回放写出 `cash_available`，资金计划优先读取账户快照现金而不是用本金倒推；`pending`/未成交回执不会写入 server-local filled 账本；2026-07-07 起 `Ashare/sim_executor.py` 自身按 A股交易日历与连续竞价时段拒绝非交易时段 server-local fill 与 Hermes pending，避免绕过 wrapper 的手工/验收调用在收盘后产生模拟成交；修复前已发生的非连续竞价 simulated 成交保留为账户事实，但复盘/看板统一归类为 `outside_ashare_regular_session` 链路验证样本，不进入策略胜率、方向命中、策略 PnL 或自我演化样本；A股资金计划已从固定集中升级为动态闸门：按候选质量、风控拒绝率、数据异常率和近期表现决定 0/1/2/3 只，强信号集中，弱信号留现金/逆回购，并把 `capital_plan` 写入模拟主循环结果、portfolio 和 `shared/review/ashare/capital_plan_YYYYMMDD.jsonl`；旧/分批持仓按唯一标的计数，模拟主循环已能生成 simulated sell 压缩单，优先处理止损、低分、机会成本和超目标持仓压缩，止损/压缩/机会成本释放资金会写入 `capital_plan.replacement_budget` 并允许同轮替换买入；新增 `shared/runtime_test/ashare_preopen_dry_run.py` 作为 08:35 盘前只读预演入口，提前验证日线覆盖、最新高流动性普通 A 股小样本的候选池、动态资金计划和执行门禁，默认样本上限 10 只且 wrapper 90 秒超时，避免盘前检查拖慢开盘，只写 runtime_test 报告，不写 `signals/`、账本、pending 或 review，异常时走系统邮件
- **执行桥**：Mac Mini `~/.hermes/` 下 Hermes 仍保留为 GUI 执行桥，只执行和回写，不做买卖判断；当前 A 股服务器本地模拟闭环不要求 mini 在线；A股健康检查已把 Hermes 降为 `mini_hermes_optional`，默认未启用时不影响主链路健康结论
- **PM（预测市场）**：多风格 simulated 扫描每 10 分钟运行；checked-in config 使用 USDC；PM sim/style 输出写入 `shared/review/pm/style_comparison.json`；`PM/probability_model.py` 是 PM 研究概率消费/融合入口，优先读取 `TRADINGAGENT_PM_MODEL_PROBABILITY_FILE` / `PM_MODEL_PROBABILITY_FILE` 或默认 `shared/review/pm/model_probabilities.jsonl` 的研究概率；没有独立研究概率时只写入 `pm_market_consensus_baseline`，即模型概率等于市场概率、`model_confidence=0`，用于说明“暂无独立 edge”，不会制造交易。2026-07-07 起 `PM/research_probability.py` 和 `job_pm_research_probability` 每 10 分钟错峰通过 MarketGraph 统一 API `GET /pm/research-probabilities` / MCP `read_pm_research_probabilities` 读取 PM 独立研究概率，再与 SharedSignals `/pm_markets` 市场元数据和 `/pm_prices` 价格快照合并写入 `shared/review/pm/model_probabilities.jsonl` 与 summary；SharedSignals 只供市场/价格数据，行内判断概率字段会被忽略。MarketGraph API 不可用、无研究概率或缺少 SharedSignals 市场价时会清空旧概率文件，避免历史 edge 残留并安全空跑；若 `/pm_markets` 市场行缺价，会读取 `/pm_prices` 最近价格补齐，但不会从 MarketGraph research row 的 `price/market_probability` 兜底。2026-07-07 生产确认 MarketGraph PM producer 正常运行但 `record_count=0`，主因是 PM 研究证据仍有样本债/方向证据不足/部分缺市场价格，TradingAgent 因此安全空跑；这不是执行器故障，也不能通过放宽阈值解决。`run_sim.py` 在无 PM 交易信号时输出结构化诊断（市场行数、可定价行数、模型概率行数、显式方向行数、策略候选数、edge 阈值和原因），用于区分 `pm_market_rows_empty`、`pm_prices_missing`、`pm_model_probability_missing` 与 `pm_model_edge_below_threshold`；`market_health` 对 PM 上游市场行/价格暂缺或模型 edge 不足标记为 warn，不再误判为执行器故障。
- **多市场**：PM/Crypto/US/HK sim executor 和 config schema 已加真实执行拒绝；US/HK simulator 入口已拒绝真实 order/account payload，fill 结果不回显 account payload；共享安全扫描递归覆盖 `direct_execution`/`real_execution`/`live` 别名；Crypto/US/HK Phase D P0 工具已独立实现；US/HK P1 report/validation/promotion 工具已补齐；Crypto/PM P1 report/validation/promotion 工具已补齐；Crypto/US/PM/HK P2 risk/portfolio/replay 工具已本地模块级实现；Crypto/PM/US 的 JSON 驱动多风格 simulated 已扩展为绩效追踪、权重调节、paused/deprecated 状态和 variant 生成闭环；HK 工具与 styles 仅保留为预留能力，默认 fail-closed，不纳入 production sim / health / evolution；基础 `styles/*.json` 已恢复为只读配置，运行态权重/状态写入 `shared/review/<market>/style_weights.json`，自动生成风格写入 `shared/review/<market>/generated_styles/`；新增 evolution guard 防止全风格亏损、组合回撤和连续多市场亏损时继续自演化；新增 `shared/execution/auto_pipeline.py` 将 universe、研究、DecisionEngine、StyleRunner 和 daily evolution 串成 simulated 自动管线；本地 production sim 层已补齐 `sim_engine`、`risk_manager`、`sim_ledger` 并接入 auto pipeline；当前生产模拟盘范围为 A股/Crypto/PM/US/CNFutures，HK 暂不接入生产调度
- **模拟资金口径**：2026-07-07 起所有生产模拟盘默认按每市场 200,000 RMB 起始资金管理；A股与 CNFutures 使用 200,000 CNY 原币资金，US/Crypto/PM 使用默认 7.2 汇率折算为 27,777.777778 USD/USDT/USDC，并在看板/汇总层统一折回 RMB 展示。多风格不是每个风格各给 20 万，而是在该市场 20 万人民币等值总账户内按 active style weight 归一化拆分；`shared/markets/sim_capital.py` 是默认资金事实源。
- **维护样本隔离**：2026-07-07 修复 US/Crypto/PM 维护重跑样本被前端计入生产模拟交易量和盈亏的问题。`trade_journal.jsonl`、`daily_mark_to_market.jsonl`、`style_performance.jsonl` 与 `style_comparison.json` 均支持 `exclude_from_dashboard=true` 或 `run_context/run_mode/run_source/sample_type` 包含 `maintenance/backfill/smoke/repair/bootstrap/dry-run` 的排除标记；`front/` 快照聚合会统一跳过这些样本。生产模拟样本默认继续计入；手动回补、烟测或修复重跑必须设置 `TRADINGAGENT_SIM_RUN_CONTEXT` 或 `TRADINGAGENT_SIM_EXCLUDE_FROM_DASHBOARD=1`，避免污染看板、复盘和演化输入。
- **多市场信号门禁**：2026-07-07 起 Crypto/US/PM/HK 通用 `run_sim.py` 不再把 SharedSignals 行情行直接转换成 `buy` 模拟信号；输入行显式带 `side/action/direction/signal/decision/recommendation=buy|sell`，或由市场专属策略生成 `signal_source=explicit_strategy_signal` 后，才会进入 StyleRunner。当前内置策略口径为 Crypto 动量突破、US 趋势跟随、PM 模型概率相对市场概率价差；价格行只用于估值/成交价。人工价格行烟测必须显式设置 `TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS=1`，且这类样本自动写入 `exclude_from_dashboard=true` 与 `sample_type=price_only_smoke`，不进入生产看板/复盘/演化口径。看板信号表会展示成交账本中的 `strategy_name`、`signal_source`、`reason` 与 `conviction`，用于证明成交来自明确策略，而不是价格行样本。
- **Crypto 空跑归因**：2026-07-07 起 `run_sim.py` 在 Crypto 无交易信号时输出结构化诊断，包含检查币种数、可定价 K 线数、显式方向行数、策略候选数、单根/回看动量阈值、每个样本的 `one_bar_return` / `lookback_return` 和原因；`market_health` 会把 `crypto_momentum_threshold_not_met` / K 线空缺识别为等待有效动量信号的 warn，不再把“策略未触发”误判为执行闭环故障。若出现策略候选但账本仍无成交，仍按 fail 处理。
- **看板交易数口径**：2026-07-07 起 `front/` 对 sim ledger replay 信号按 `market + symbol + status + stage` 去重，市场级 `tradeCount` 不再用风格层 `filled_count` 放大；多风格子账户同一标的的成交仍保留在 style comparison 的 `filledCount` 中，用户看板的市场摘要按唯一市场机会展示。`StyleRunner` 与 `SimLedger` 会把 `strategy_name`、`signal_source`、`reason`、`conviction/score` 等策略来源写入成交账本，便于复盘证明成交来自明确策略。
- **A股权益曲线口径**：2026-07-07 修复 `shared/review/equity_snapshots.py` 将 A股本地模拟 `cash_available` 误当 `capital_base` 的问题；A股 server-local 快照现在按 200,000 初始资金重放成交现金流，`total_equity = cash + market_value`，`total_pnl = realized + unrealized`，避免看板在买入后把现金余额误读为账户总权益。
- **实盘安全基础设施**：新增 `shared/execution/real_trading_gate.py` 与 `signals_real.py`，真实交易默认拒绝，必须显式环境开关、人工确认 token、资金上限、交易时段、T+1 与 halt 检查全部通过；sim → real promotion 只接受经 sim 审计的来源；`signals/real/*` 为隔离队列，不代表自动下单或已成交
- **模拟撮合引擎**：`shared/execution/sim_engine.py` 已进入 Phase 1，统一支持 bid/ask marketability、盘口量/5m bar volume 部分成交、A股买入整手、A股 T+1 可卖数量、A股涨跌停边界、PM 概率价格边界、现金可用性检查和轻量对手盘环境参数；A股 server-local 模拟执行与 `auto_pipeline` 本地 fallback 已接入该引擎；A股执行器会从 server-local 模拟账本补齐 `cash_available` 与 T+1 `sellable_qty`，`auto_pipeline` 会从 SharedSignals reader 的 5分钟/日线 bars 生成 `market_snapshot`；该层仍是 paper-only，不接真实券商/交易所撮合。
- **CNFutures 模拟盘**：国内期货只跑模拟盘，无单独影子盘；多风格模拟会写 `shared/review/data/cn_futures_sim_reviews.jsonl`，并同步输出 `shared/review/cn_futures/style_comparison.json`、`style_performance.jsonl`、`observation_report.json` 与盘后 `win_rate_calibration_report.{json,md}` 供现有看板/巡检接入；观察报告已提供 `dashboard` 和 `next_validation` 顶层字段，便于看板直接展示 readiness、下一交易时段验收步骤和是否继续累计样本；`signals/positions/cn_futures_sim_positions.json` 的模拟持仓快照已接入生产前端 holdings 解析；`score_summary`、`error_summary`、`style_health`、`hold_count`、`hold_reason_summary`、`forward_label_summary` 和 `dynamic_threshold_candidates` 标记样本不足、手续费、保证金占用、名义金额、可用 PnL 样本、风格状态、风控拒绝原因、主动不交易原因、前向标签和动态阈值候选；`CNFutures/live_gateway.py` 为未来 CTP/期货公司接入预留 fail-closed 占位，当前拒绝全部真实期货订单；生产 crontab 已改为期货日盘/夜盘 5 分钟级运行 `job_cn_futures_sim.sh`，并读取 SharedSignals `market_bars_intraday` 的 Futures 5 分钟数据；5 分钟 runner 已加入 10 分钟默认数据新鲜度闸门、同风格/同合约连续同方向重复暴露限制、tick/slippage 成交价、静态涨跌幅边界、bar volume 部分成交、模拟持仓快照、风格保证金 cap、不过夜强制平仓、换月保护和反向平仓 PnL 估算；2026-07-08 起换月保护按合约月开始日前后配置窗口禁止新开仓，已进入合约月后的临近交割合约也会被拦截；2026-07-07 起 5 分钟 runner 调用 `get_bars_intraday` 时显式传入交易日 start/end，并且 intraday universe 优先读取 SharedSignals 当日 `market_bars_intraday` 最新一批 5分钟 bar 中的可执行合约，拒绝 `CU.SHF` 这类泛合约、仅有历史 bar 的旧合约和早一批已滞后的合约，避免开盘后把非目标交易日分钟线、过期合约或 stale 合约误当作可交易候选；盘前验收已复用同一可执行合约过滤并报告 raw/executable symbol、产品覆盖、5分钟 read model 可达性和运行时风格状态；闭市时段的盘中 runner 会写正常 `market_closed` 复盘行，不再把收盘后的最后一根有效 5 分钟 bar 误报为 `stale_intraday_bar`，但交易时段内真正滞后的分钟线仍会 fail/warn；手续费模型已显式区分 `rate` 与 `fixed_per_lot`，不再靠费率数值大小推断；非指数基础风格默认拒绝夜盘 bar，只有显式 `night_session_allowed=true` 才允许夜盘模拟；`CN_FUTURES_SIM_DISABLED=1` 可临时暂停模拟任务但保留观察报告；`index_intraday_directional` 已加日盘-only、趋势一致、成交量确认、开盘冷却、跳空冷却、低波动、方向连续性、最新 bar 反转、信号噪声比、bar gap、K线实体质量、连续同向 bars 和 late-chase 过滤，并输出场景标签与模拟出场计划，演化器按 `win_rate_first_risk_adjusted` 目标生成小型候选族群；force-flatten 平仓现在按已有持仓成本和实际成交价写 realized PnL，`score_records` 增加 `pnl_attribution`，可区分 `no_closed_pnl`、样本不足和真实已实现收益。
- **CNFutures 冷启动样本保护**：2026-07-06 修复样本不足导致全部风格被 runtime overlay 标记为 `paused/enabled=false` 后模拟器永久空跑的问题；`sample_insufficient` 现在保持 `active/observe`，继续在 simulated-only 层积累样本，不允许晋升为真实交易。真正 `blocked/deprecated/disabled` 或会话不允许的风格会写入 `hold_reason_summary`（如 `style_paused`、`style_session_not_allowed`），避免以后出现“cron 正常但无成交、无原因”的假正常状态。
- **cron 解耦入口**：Crypto/US/PM 5 分钟模拟 cron 已安装；PM 独立研究概率 `job_pm_research_probability` 按 `2-59/10` 每 10 分钟错峰刷新，先于后续 PM 5 分钟 sim 使用，不写交易队列；A股工作日交易时段 5 分钟级模拟 cron 已安装且默认服务器本地执行，A股开盘验收 cron 已安装：08:35 盘前 dry-run 预演数据→候选池→资金计划→执行门禁、08:55 盘前数据验收、09:35/13:05 数据验收、09:45/13:10 首样本告警；A股只读研究证据 cron 已加入 09:26/14:56/15:10，生成集合竞价、尾盘动能、204001 逆回购估算和风格证据面板输入，不写交易队列；CNFutures 5 分钟模拟 cron 已安装并相对 SharedSignals 采集错后 1 分钟，观察报告错后 2 分钟刷新，风格演化按日盘/夜盘 30 分钟级运行，盘后胜率校准报告在 15:45 与 02:45 触发，开盘前只读验收在 08:55/12:55/20:55 触发，开盘后数据验收在 09:05/13:05/21:05 与 00:35 触发，首样本告警在 09:10/13:10/21:10 与 00:40 触发；`job_opening_acceptance.sh` 作为 A股+CNFutures+SharedSignals+模拟盘总验收入口，08:56、09:06/09:45、13:06/13:45、20:56、21:06/21:45、00:41 只读运行并输出短文本结论；HK 5 分钟模拟 cron 已按 Nicholas 最新决策停用且 wrapper 默认需要 `TRADINGAGENT_HK_SIM_ENABLED=1` 才能运行；`shared/wrappers/job_sim_market_health.sh` 每 10 分钟只读巡检 A股/Crypto/PM/US/CNFutures 模拟闭环，并写出 `shared/runtime_test/sim_market_health_latest.json` 供看板读取当前运行状态；`shared/wrappers/job_equity_snapshots.sh` 每 5 分钟追加模拟账本权益快照，供前端实时收益曲线使用，不写交易队列；`job_style_evolution` 模板每 4 小时只跑 Crypto/PM/US simulated 演化；`cron/daily_review.sh` 16:00 做复盘与演化摘要；`cron/health_check.sh` 上报 SharedSignals/TradingAgent/MarketGraph 统一健康，并调用 MarketGraph `deploy/install_combined_crontab.sh --check` 验证 SharedSignals/TradingAgent/MarketGraph 三系统 live crontab 覆盖；关键 wrapper 的 early setup stderr 已进入各自 `shared/logs/cron/*.log`，便于定位 env/source 启动失败；均带 flock 与独立日志
- **生产运行用户**：TradingAgent cron 由 `marketgraph` 用户运行；2026-07-07 已修复 root 手动烟测造成的 `job_pm_research_probability` 日志、PM research probability 输出和 `/opt/investment/MarketGraphRuntime/tradings/state` lock 文件归属漂移。后续生产烟测若必须用 root 发起，应立即用 `marketgraph` 身份复跑或恢复运行态目录归属，避免 cron 触发但写入失败。
- **SharedSignals API 消费**：`SharedSignalsAPIClient` 已覆盖核心 16 个数据消费端点；`TradingagentDataReader` 已对核心读取路径启用 API-first，SQLite 只读回退保留；A股 `get_assets()` 走 SharedSignals `stock_basic` read model，单日 `get_bars_daily()` 会补齐 start=end；`get_bars_intraday()` 已优先走 SharedSignals `/realtime_5min?market=...` 并保留 SQLite 回退，期货/A股 5 分钟行情与可选 bid/ask 字段可走同一消费入口；PM `get_pm_prices()` 已接入 SharedSignals `/pm_prices` 作为 `/pm_markets` 缺价时的价格快照补充；SQLite 回退兼容 SharedSignals read model 中 `interval=5min` 与调用侧 `5m` 的差异，并支持 `YYYYMMDD` 日期读取 `YYYY-MM-DD HH:MM:SS` 的 bar_time；5 分钟 `run_sim.py` 已从直接 SQLite 读取改为 SharedSignals reader/API-first，2026-07-04 已验证 crypto=5、PM=10、US=9 条模拟信号；HK 数据与模拟入口保留但暂不进入生产调度
- **数据源边界复核**：2026-07-04 主服务器生产路径审计未发现 TradingAgent 活动代码直接调用 Tushare/Binance/Polymarket/Alpaca/Yahoo 等行情源；HTTP 调用保留在 SharedSignals API 客户端、健康检查、邮件/webhook 和研究 LLM 路径。误拷贝的 untracked `Users/` 旧目录已从服务器删除，`.gitignore` 已防止再次出现。
- **旧市场工具目录退役口径**：2026-07-05 开盘前复核已修正 `US/AGENTS.md` 与 `PM/AGENTS.md`；旧 `/opt/investment/US/tools/`、`/opt/investment/PredictionMarkets/tools/` 只保留为历史迁移线索，不是现役生产代码、采集或执行入口。
- **旧 cron 迁移快照退役**：根目录 `cron_gap.md` 已移入 `docs/archive/cron_gap_20260629.md`，只作历史参考；当前 cron 依据为 `STATUS.md`、仓库 `crontab.txt`、`shared/wrappers/` 和服务器 live crontab。
- **研究/筛选增强**：新增 `shared/screening/fundamental_analyzer.py` 和 `shared/research/multi_perspective.py`，只读消费 SharedSignals API/DB，输出基本面质量分、同业比较、red flags 和 bull/bear/macro/technical 多视角共识报告；`auto_pipeline` 消费这些研究结果生成 simulated 决策，不触碰实盘队列
- **复盘节奏**：11:45 午盘 / 15:30 收盘 / 22:00 夜间校准 / 07:30 晨报
- **复盘/报告输入**：日报、周报、归因和汇总邮件默认通过 `load_review_trades()` 读取 legacy shadow fills + `shared/logs/sim_ledger/<market>/<style>/trade_journal.jsonl` + A股 `shared/logs/local_sim/local_sim_trades.jsonl`；报告保留 `review_trade_count`、`shadow_trade_count`、`simulated_trade_count` 三个计数，避免服务器本地模拟成交被误判为无样本
- **影子盘状态闭环**：US/Crypto/PM/HK 本地 shadow runner 的 `simulated_fill.status=filled|partial` 会立即推进到 `signals/shadow/filled`；若状态机推进失败，卡片进入 `signals/shadow/failed` 并保留 `settlement_warning`，不再把已模拟成交卡片长期留在 `shadow/pending`
- **A股本地模拟回执**：`local_sim_ledger` 在写入 server-local simulated trade、positions、PnL 和 `signals/positions/simulated_ashare_positions.json` 的同时，会追加带 `receipt_sha256` 的 `signals/sim_execution_receipts.jsonl`；健康检查默认读取 TradingAgent 本地回执，旧 MarketGraph 回执只在历史文件存在时作为兼容输入，并能识别“尚无首笔本地模拟成交”的 bootstrap 状态，避免把无样本误报为链路故障
- **模拟盘健康检查**：`market_health` 已区分交易时段样本缺失与闭市等待首样本；A股通过 `Ashare.t_plus_1.is_trading_day()` 判断真实交易日，法定节假日不会仅因工作日误判为应有样本；A股和 CNFutures 在周末/闭市且尚未进入应产生样本的时段时不再误报 warn，进入或经过交易时段后仍无数据/成交会继续告警；A股健康检查已补充 stale 执行卡、server-local 账本/持仓快照数量与现金一致性、最新 `capital_plan` 看到的持仓数与快照持仓数对账。
- **A股/CNFutures 开盘验收框架**：A股新增 `shared/runtime_test/ashare_opening_validator.py`，提供 `validate_pre_open` / `validate_opening` / `first_sample_alerts` 三个只读入口，验证 SharedSignals 日线/5分钟数据、本地模拟成交样本、签名回执、复盘日志和 filled signal cards；`first_sample_alerts` 会输出 `no_trade_explanation`，并只按当天本地模拟成交计数，避免旧成交掩盖当天未交易原因；不存在的旧 `a_share_no_trade_attribution.py` manifest 入口已退役。新增 `shared/wrappers/job_ashare_pre_open_validation.sh`、`job_ashare_opening_validation.sh`、`job_ashare_first_sample_alert.sh` 三个 wrapper，并已写入生产 crontab。CNFutures `opening_validator.py` 已增强 filled signals、receipts 和 review rows 计数与告警，并输出 `opening_30m_review` 标准块，用于区分开盘 30 分钟仍在积累样本、缺 5 分钟数据、无模拟成交和缺回执。`shared/runtime_test/opening_acceptance.py` 聚合 SharedSignals API、watchdog 输入、halt 文件、模拟盘总巡检、A股验收和 CNFutures 验收，默认输出短文本，`--json` 输出机器可读结果。所有开盘验收均固定 `real_trading_enabled=false`，只读执行。
- **CNFutures 验收入口兼容**：2026-07-05 复核发现 `CNFutures/opening_validator.py` 直接脚本启动会被相对导入阻断；已补兼容，`python -m CNFutures.opening_validator` 与 `python CNFutures/opening_validator.py` 均可用于只读验收。生产 wrapper 仍使用模块启动。
- **多市场绩效去重**：Crypto/PM/US/CNFutures 共用的 `style_performance.jsonl` 已从 5 分钟 append-only 改为按 `(market, style_name, date)` 幂等写入，历史读取也会取同键最新值，避免 5 分钟任务把 runs/trades/PnL 重复放大并污染风格演化。
- **多市场收益口径**：新增 `shared/review/pnl_summary.py` 统一摘要层，按 `realized_pnl + mark-to-market unrealized_pnl` 聚合 Ashare/Crypto/PM/US/CNFutures 模拟账本；Ashare 用 SharedSignals 日收盘价做 mark-to-market（缺失则回退成交价），其他市场用 `SimLedger` journal 重放盯市；PM 持仓 now 按 `market_id + outcome` 区分 YES/NO，NO 持仓按显式 `no_price` 或 `1 - yes_price` 估值，避免把 NO 成本与 YES 市价相减造成虚假高浮盈；PM 模拟成交层在 price history 缺失时会从 SharedSignals `/pm_markets` 当前行取 YES/NO 价格，不再统一按 0.5 熵值兜底成交；日报、周报、运维报告、`market_health` 和 `metrics_dashboard` 均输出 `ledger_realized_pnl` / `ledger_unrealized_pnl` / `ledger_total_pnl` / `ledger_market_value` / `ledger_open_position_count` / `ledger_missing_mark_count` / `ledger_pnl_source`；A股额外输出 `strategy_total_pnl`、`strategy_market_value`、`strategy_open_position_count` 与 `sample_quality`，用于区分真实账户账本结果和可用于策略评价的样本；Crypto/PM/US 的 `StyleRunner` 主收益口径同样基于统一模拟账本；CNFutures `sim_runner.py` 和 `review.py` 已补 unrealized 输出；HK 仍暂停，不纳入本口径。
- **服务端**：阿里云华南3/广州 `8.138.181.177`，生产路径 `/opt/investment/tradingagent/`
- **运行监控**：每小时运维报告（`ops_report.py`），覆盖执行队列、sim 队列、回执完整性、PnL 摘要
- **邮件模板**：11 类 TradingAgent 邮件已统一为移动端 30 秒决策版，顶部决策条、交易执行边界、三张摘要卡和日报/周报 inline SVG 图表已补齐；通道映射未变
- **前端/看板入口**：唯一活跃生产前端是本仓库 `front/`，生产服务 `tradingagent-front-api.service` 指向 `/opt/investment/tradingagent/front`；快照 API 同时支持 `/healthz` 与 `/health` 运维探针。独立 `TradingAgentDashboard` 原型不再作为开发、部署或文档入口。首页以实时收益、机会管道和下一步关注为核心，避免在右栏重复展示收益/账户/风险数字；机会管道优先读取 `funnelEvents`，展示“机会进入 → 初筛 → 研究 → 风控 → 待执行 → 成交/观察/复盘/放弃”的动态流动，没有事件时才回退到信号阶段推导，避免把已成交账本回放误当成当前筛选转化率。收益页的累计收益曲线支持“今日/7日/30日/全部”切换，图表只负责走势和事件点，当前收益、目标差、回撤等权威数字由页面摘要板/实时收益卡承载，不在同一面板重复展示。收益曲线优先读取模拟账本权益快照 `shared/logs/sim_ledger/*/*/daily_mark_to_market.jsonl`，该快照由 `shared/runtime_test/write_equity_snapshots.py` 追加写入，字段包含本金、权益、已实现/未实现收益、回撤、交易数、价格缺失状态、原始币种、`fx_to_cny` 与 CNY 折算字段；前端 API 会按 5 分钟 bucket 汇总为整盘收益，最多保留 360 个点，支撑今日/7日/30日曲线查看，默认全部按 RMB 展示，避免跨 US/USDT/USDC/CNY 直接混合；持仓面板同时读取 `signals/positions/*.json`，已兼容 CNFutures `positions[]` 快照；信号表展示策略来源列，优先显示账本中的 `strategy_name` 与 `signal_source`，市场摘要读取 30 分钟内的 `shared/runtime_test/sim_market_health_latest.json`，把 Crypto/PM 的“策略等待”和执行故障分开；健康 latest 过期时回退到账本/风格证据，避免旧健康结论覆盖当前看板。市场摘要仍会读取当天 `shared/logs/ashare_no_trade_explanations.jsonl` 并展示 A股无交易原因和下一步检查方向；缺少快照时才回退到日复盘 return 字段或按日 style performance。默认本地 fallback 不再展示暂停的 HK 样例，改用 CNFutures simulated-only 样例；真实 sim ledger 默认也跳过 HK，只有显式 `TRADINGAGENT_HK_SIM_ENABLED=1` 才读取港股旧账本。
- **A股收益看板口径**：A股权益快照只接受 canonical `ashare/ashare_sim`，由 server-local `shared/logs/local_sim` 账本生成；旧 `ashare/<style>` 多风格测试账本不再进入 dashboard 汇总，避免 20k/16.6k 历史样本污染当前 200,000 元模拟盘口径。
- **A股复盘样本口径**：A股日报/周报/归因默认只读取 server-local `shared/logs/local_sim/local_sim_trades.jsonl`；旧 `shared/logs/sim_ledger/ashare/<style>/trade_journal.jsonl` 风格账本视为退役历史样本，不再进入默认复盘输入。
- **A股开盘验收与无交易分层**：`ashare_opening_validator` 的 first-sample 报告已把 5分钟 bar、信号状态、服务器本地模拟成交、签名回执、复盘行数和 no-trade 分类汇总到同一报告；若当天没有交易，会优先读取最新 `shared/logs/ashare_no_trade_explanations.jsonl`，把无候选、无信号卡、风控全拒、资金/组合构建阻塞、重复幂等、执行跳过、执行失败、回执缺失和复盘待生成区分开。缺回执只在当天已经出现服务器本地模拟成交后才告警，避免把“尚无成交”误报成“成交后缺回执”。`opening_acceptance.py` 短文本同步展示 bar/信号/成交/回执/复盘计数。
- **盘前验收修复**：2026-07-07 已修复 `job_opening_acceptance` 生产 cron 依赖当前工作目录导致寻找 `/home/marketgraph/shared/...` 失败的问题，wrapper 会强制切到 `TRADINGAGENT_ROOT` 并使用绝对脚本路径；统一开盘验收会写 `shared/runtime_test/opening_acceptance_latest.json` 与 history，并在 warn/fail 时走系统通道邮件，cron 使用 `--exit-zero` 避免同一异常重试三次刷屏。SharedSignals 盘前核心 API 探针改用轻量 `/cache/status` + `/capabilities`，`/health` 超时只作为降级项；A股盘前日线检查新增 `latest_daily_age_days`，日线过旧会提前 warn，不再只因历史日线数量足够而误判通过。
- **A股旧测试账本归档**：新增 `shared/runtime_test/archive_ashare_legacy_ledgers.py`，只归档 `shared/logs/sim_ledger/ashare/*` 中非 canonical `ashare_sim` 的旧风格账本，默认 dry run，`--apply` 时移动到 `shared/logs/archive/ashare_legacy_style_ledgers/<batch>` 并写 manifest；不得删除或归档 `ashare_sim`。旧样本不再作为活跃输入，确认归档 manifest 后可按批次永久删除归档副本。
- **A股只读研究证据**：`Ashare/research_evidence.py` 与 `job_ashare_research_evidence.sh` 统一输出 opening auction 异常、closing momentum 候选、204001 逆回购预估收益和 A股风格证据；集合竞价缺少 09:15-09:25 数据时会显式标记 `first_5m_proxy`，标的选择会优先读取 SharedSignals 当日 `rt_min/stk_mins` 已有分钟线的样本，再回退资产表，避免把“扫错无分钟线样本”误判为全市场无数据；204001 优先读取 SharedSignals `/market_data` 日线收益率，尾盘候选带 `next_trading_day` 与 open/high/close 兑现标签（数据未到时 `pending_next_day_bar`），风格资金按 `shared/review/ashare/style_weights.json` 运行时 active 权重切分 200,000 元虚拟预算；结果写入 `shared/review/ashare/research_evidence_latest.json` 与 append-only `research_evidence.jsonl`，固定 `read_only=true`、`real_trading_enabled=false`，不进入 simulated/real 执行队列。
- **A股六维评分供数**：2026-07-07 生产隔离验收发现 A股 500 个评分样本六维全部为 0.5，直接原因是 SharedSignals P0 日线历史仅 7 个交易日、`/fundamentals`/`/capital_flow`/`/macro` 当前无可用行，且 TradingAgent 旧 `get_factors()` 没有对 `ashare → Ashare` 做 canonical 查询。已修复 `get_factors()` 的 A股市场名/代码双格式兜底，并让 `six_dimension_scorer` 在旧 factors 为空时消费 SharedSignals `/fundamentals` 与 `/capital_flow` read model 行；技术维度仍要求足够日线历史，不用 7 根 K 线硬凑信号。SharedSignals 已把 P0 `daily/stk_factor/stk_factor_pro` 评分相关接口窗口提高到 90 天，并把 `moneyflow` 调整为 P1 盘后全市场日频采集。TradingAgent 资金维度只使用资金金额字段（如 `net_mf_amount`），不再把 `net_mf_vol` 这类成交量混入金额分；候选池和轮动条件已统一识别 `api_name:metric` 因子名前缀。需等待生产 moneyflow 回补后复验 `score_diagnostics.neutral_default_like_dimension_counts` 不再全等于 `scored_count`。

## 二、已知问题

- HK 按 Nicholas 最新决策暂不接入生产模拟盘；`hk_basic` 正常但 `hk_daily` 当前不作为生产模拟输入。HK 代码、wrapper 和数据诊断保留，默认不跑 cron、不纳入多市场健康/evolution 结论；手动运行也需要显式 `TRADINGAGENT_HK_SIM_ENABLED=1`，HSI 代理回退需要额外 `SIM_HK_PROXY_ENABLED=1`。
- A股 2026-07-06 真实交易时段恢复验收已通过：SharedSignals 当日 5 分钟线已落入 read model；TradingAgent 修复了 `5m/5min`、日期格式、A股日线回看、盘中价格取值、买入整手和健康检查误报后，服务器本地模拟盘已产生 server-local filled、签名回执、持仓快照和成交回执邮件；后续已修正候选质量缺陷：先对 20 个候选打分，再按 combined score 排序进入 A股动态资金计划，`max_portfolio_positions=3` 作为上限而非硬买目标；同轮被价格、风控或执行跳过的候选会写 `shared/review/ashare/execution_exclusions_YYYYMMDD.jsonl` 并进入日报 `execution_quality`；同轮资金计划会写 `shared/review/ashare/capital_plan_YYYYMMDD.jsonl`，超目标旧持仓、止损持仓和轻量机会成本持仓会进入 `rebalance.sells` 并按 simulated sell 路径执行，计划卖出释放的资金会进入 `replacement_budget` 以避免满仓止损后只卖不换。
- A股本地模拟回执链路已具备签名回执文件；生产环境仍需等待下一次真实交易时段产生真实生产样本，用于验证真实 cron 样本写入和收益复盘质量。健康检查已能区分“无首笔成交样本”和“有失败/有成交但缺回执”，后者才会告警。
- Hermes/Mini GUI 路径已按 Nicholas 最新要求搁置为第二选择；只有未来显式启用 `ASHARE_SIM_HERMES_ENABLED=1` 时才需要重新验证 mini health、同花顺按钮识别、截图回执和账户同步。
- 多市场旧系统 symlink 依赖已全部清除（61 个死 symlink）；工具独立实现已完成，剩余风险在 A股下一个交易日生产样本与晋降级/guard 的持续运行验证
- 集合竞价已进入只读研究证据层；SharedSignals 若没有 09:15-09:25 竞价 bars，会输出 `no_auction_data` 而不是伪造信号。该层不接模拟/实盘执行。
- A 股实盘路径仍是人工；当前只补齐本地 fail-closed 安全门和 `signals/real/*` 隔离队列，未部署为自动下单路径
- 尾盘动能风格 `Ashare/styles/closing_momentum.json` 已作为 research style 预留并保持 `paused`；只读研究证据已可输出 14:40-14:56 候选、次交易日 open/high/close 兑现标签和 204001 现金管理估算。激活前仍需要累计足够样本并通过阈值复核。
- A股研究证据的当日分钟线依赖 SharedSignals P0 `rt_min/stk_mins` 入库；若开盘后仍无当日 5 分钟数据，问题应归因到 SharedSignals 采集/bridge/cron，而不是 TradingAgent 执行队列。
- PM 当前安全空跑的生产阻塞在 MarketGraph 研究概率 read model 的样本债和可匹配市场价格/方向证据不足；TradingAgent 已按 MarketGraph API/read model 消费，不应从 SharedSignals PM 行内字段生成判断概率，也不应放宽 PM edge 阈值来制造交易。

## 三、下一步

1. [x] **P2：Crypto/US/PM/HK 多市场工具独立实现** — Crypto risk/portfolio/replay、US portfolio/replay、PM risk、HK portfolio 已补齐；HK 工具保留但暂不接入生产模拟调度
2. [ ] **P2：多市场模拟盘生产闭环** — 服务器侧 A股/Crypto/PM/US simulated cron、SharedSignals reader/API-first、统一账本、日报/周报复盘读取和健康检查已完成首轮验证；剩余为 A股下一个交易日生产样本、promotion/权重演化/guard halt-thaw 的持续运行验证
3. [ ] **P2：A 股实盘路径设计** — 需先确认安全边界和人工确认环节
4. [x] **P2：SharedSignals HTTP API 消费迁移** — 15/15 端点客户端已完成；`TradingagentDataReader` 已对 `get_market_data` / `get_events` / `is_trading_day` / `get_bars_intraday` 接入 API-first 访问；SQLite 只读回退保留
5. [ ] **A股/CNFutures 下一个真实交易时段开盘验收** — A股新增 `shared/runtime_test/ashare_opening_validator.py` 与三个 wrapper（pre_open / opening / first_sample_alert），并已写入生产 crontab；只读验证 SharedSignals 日线/5分钟数据、本地模拟成交、签名回执、复盘日志和 filled signals，异常才发系统告警；CNFutures `opening_validator.py` 已增强 filled/receipt/review 样本告警；两者均固定 `real_trading_enabled=false`，等待下一交易日生产样本验证。

### 2026-07-05 opening validation residual fixes

- [x] TradingAgent front snapshot API 同时支持 `/healthz` 与 `/health`，方便 systemd、反代或外部监控统一探针。
- [x] TradingAgent front 默认 fallback 数据移除暂停 HK 信号、持仓和分配样例，改用 CNFutures simulated-only 样例，避免真实 snapshot 缺失时页面误展示港股生产机会。
- [x] A股 `first_sample_alerts` 新增 `no_trade_explanation`，把没有交易拆成数据读取失败、未到首样本窗口、无 5 分钟数据、覆盖不足、无信号/全被风控拒绝、执行缺失、回执缺失、复盘待生成和闭环 ready；本地模拟成交只按当天日期计数。A股 simulated orchestrator 每轮也会返回并在无成交时追加 `shared/logs/ashare_no_trade_explanations.jsonl`，记录候选数、订单数、风控拒绝、价格/手数跳过、重复信号、执行失败/挂起等漏斗原因，方便盘后复盘。
- [x] CNFutures `first_sample_alerts` 新增 `opening_30m_review`，开盘 30 分钟前标记为累计样本，30 分钟后若仍无 5 分钟数据、模拟成交或回执会给出标准原因和下一步。
- [x] CNFutures 日盘信号时间桶和开盘冷却从真实 09:00 起算，09:00-09:30 标记为 `day_open_first_30m`，避免遗漏期货开盘前 30 分钟行为。
- [x] A股模拟健康检查接真实交易日历，法定节假日不会仅因周一至周五而预期产生当天样本。
- [x] CNFutures 同日反向成交 realized PnL 不再重复扣两次 round-trip 手续费；上一笔成交已预扣开平仓估算费时，反向平仓只使用这一笔 round-trip fee，避免演化/复盘样本过度悲观。
- [x] A股 manifest 删除不存在的旧 `a_share_no_trade_attribution.py` 入口；无交易归因以后从开盘首样本验收报告读取，不再维护第二套入口。

### 2026-07-05 CNFutures observation, win-rate filters, and faster simulation evolution

- [x] `CNFutures/observation_report.py` 新增只读 5 分钟交易观察报告，汇总数据新鲜度、最新模拟样本、风格排名、运行态权重、生成变体和告警，可供内置 `front/` 后续接入。
- [x] `CNFutures/opening_validator.py` 新增只读开盘验收入口，验证当前日盘/夜盘开盘后 Futures 5 分钟 bar 是否落地以及合约覆盖数。
- [x] `shared/runtime_test/cn_futures_live_check.py` 新增 `observation_phase` 与 `alerts`，把“等待 5 分钟数据 / 等待模拟样本 / 等待风格复盘 / ready / blocked”直接暴露给看板和运维。
- [x] `shared/runtime_test/cn_futures_live_check.py` 新增 `next_validation`，把下一个交易时段要验收的 fresh 5分钟数据、模拟复盘样本、风格输出和实盘关闭状态显式化。
- [x] `CNFutures/observation_report.py` 新增看板稳定字段：`schema_version`、`dashboard` 和 `next_validation`；UI 可直接读 readiness、top style、filled count、latest bar 和下一步。
- [x] CNFutures 复盘和 run summary 会输出 `cadence`、`latest_bar_time` 和 `real_trading_enabled=false`；live check 对 5分钟成交缺少 bar_time 或误带实盘标记的样本会告警。
- [x] `CNFutures/opening_validator.py` 输出已明确 `data_source="SharedSignals read_model"` 和 `read_only=true`，避免被误解为 TradingAgent 自采集。
- [x] `CNFutures/opening_validator.py` 新增 `--pre-open` 与 `--first-sample` 两个只读模式，分别用于开盘前 SharedSignals 日线可用性验收和开盘后首个 5 分钟样本/模拟样本告警；新增 wrapper `job_cn_futures_pre_open_validation.sh` 与 `job_cn_futures_first_sample_alert.sh`。
- [x] `CNFutures/review.py`、`sim_runner.py`、`run_simulation.py` 和 `observation_report.py` 已记录并暴露 `hold_count`、`hold_reason_summary` 与看板 `top_hold_reason`，用于区分策略主动不交易与真实数据/执行缺口。
- [x] `index_intraday_directional` 已新增模拟复盘字段：确认信号输出 `scenario_tags` 与 `exit_plan`；runner 对每笔记录写入 `forward_outcome`、`scenario_tags` 和 `exit_plan`；review 输出 `forward_label_summary` 与 `dynamic_threshold_candidates`；observation report 暴露 forward labeled/pending 计数和动态阈值候选数量。
- [x] `CNFutures/calibration.py` 新增盘后轻量校准：读取 CNFutures simulated filled/partial signal cards 和 SharedSignals 5分钟 bars，写 `forward_labels.jsonl` 与 `win_rate_calibration_report.{json,md}`；新增 wrapper `job_cn_futures_calibration_report.sh` 与日盘/夜盘盘后 cron。
- [x] `index_intraday_directional` 胜率质量过滤已增强：默认要求日盘-only、不过夜、动量与均线方向一致、`min_volume_ratio=1.05` 成交量确认，并新增开盘 15 分钟冷却、1% 以上跳空 30 分钟冷却、低于 0.1% 近期波动过滤、方向连续性过滤、最新 bar 反转过滤、信号噪声比过滤、bar gap 过滤、K线实体质量过滤、连续同向 bars 过滤和 late-chase 过滤；弱确认信号转为 `hold`；非指数基础风格默认拒绝夜盘 bar，避免夜盘未充分验证时误开仓。
- [x] `CNFutures/evolution.py` 从单一变体生成升级为小型参数族群，优秀风格可按 `precision/fast/smooth` 并行生成最多 3 个 simulated-only 候选，目标为 `win_rate_first_risk_adjusted`。
- [x] `CN_FUTURES_SIM_DISABLED=1` 可暂停期货模拟 runner，并输出 `state=paused`、`real_trading_enabled=false` 的 JSON 摘要；cron 模板已补期货演化和开盘验收固定入口。

### 2026-07-05 Crypto/PM/US simulated performance dedupe

- [x] `shared/markets/performance_tracker.py` 的 `save_run()` 改为同一市场/风格/日期幂等写入，`load_history()` 与 `compare_styles()` 读取时也会自动折叠旧重复行，防止 5 分钟任务把当日重复样本累计成虚假的 runs/trades/PnL。
- [x] 生产侧已备份并压缩 `shared/review/{crypto,pm,us}/style_performance.jsonl` 历史重复数据；压缩后 crypto=13、pm=11、us=6 条唯一市场/风格/日期记录。
- [x] 回归测试覆盖同日同风格重复写入、旧 JSONL 压缩和 evolution/runtime style 兼容；当前 Crypto/PM/US 模拟链路 health 为 pass。
- [x] `shared/accounting/sim_ledger.py` 新增 `total_pnl()`，按成交账本的 FIFO realized PnL 和最新信号价格对持仓 mark-to-market；缺价格时按成本价保守盯市并输出 `missing_mark_count`。
- [x] `shared/markets/style_runner.py` 不再用 `_estimate_pnl()` 作为 Crypto/PM/US 主收益口径；风格层 `pnl` 改为 `realized_pnl + unrealized_pnl`，并保留 cash/market_value/equity 供看板和 evolution 使用。
- [x] 回归测试覆盖买入后持仓浮盈、卖出后已实现收益 + 剩余持仓浮盈、缺失盯市价格按成本价保守处理。
- [x] win_rate/max_dd/sharpe 已改为优先基于模拟账本的已实现收益和持仓盯市样本计算；没有账本样本时才回退单次成交 PnL。
- [x] `job_cn_futures_observation_report.sh` 已接入日盘/夜盘固定 cron，模拟任务后刷新只读 `shared/review/cn_futures/observation_report.json`；`job_cn_futures_opening_validation.sh` 已加入开盘验收 cron。

### 2026-07-05 CNFutures execution realism

- [x] `CNFutures/sim_executor.py` 不再理想化按信号价全额成交：模拟成交价会按 tick size 取整、加入 slippage bps，并按静态涨跌幅边界拒绝离谱价格。
- [x] 模拟成交已使用 5 分钟 bar volume 和 `volume_participation` 限制可成交手数，超出部分返回 `partial`，并落到 `signals/partial` 状态目录。
- [x] 保证金、名义金额和手续费改为基于实际模拟成交价与实际成交手数计算。
- [x] `CNFutures/sim_runner.py` 在同风格同合约反向成交时估算 round-trip realized PnL，供复盘胜率和演化样本使用；当前仍是同日反向信号估算，不等同真实 CTP 持仓回报。
- [x] 新增 `signals/positions/cn_futures_sim_positions.json` 模拟持仓快照；新开仓会检查同风格现有保证金占用，超过 `max_margin_usage` 时拒绝。
- [x] `no_overnight` 风格在日盘收盘前窗口生成 simulated flatten 单；`rollover_min_days_to_contract_month_start` 可阻止临近/进入合约月的新开仓。
- [x] 若 5 分钟 bar/order 带一级 bid/ask 与可用量，模拟成交会用 buy→ask、sell→bid，并用对应盘口量限制成交；显式 `last_trade_date` / `expiry_date` 会触发到期保护。
- [x] SharedSignals 5 分钟期货字段已在 TradingAgent reader、CNFutures order 和 simulated receipt 中完整留痕：`bid_price/ask_price/bid_size/ask_size/last_trade_date/expiry_date`；成功成交与 partial 回执均保留字段，便于复盘和看板解释成交价、成交量限制与合约到期保护。

## 四、活跃任务

（当前无活跃迁移任务）

## 五、最近完成

### 2026-07-07 A股 evidence reason diagnostics + CNFutures pre-open/PnL

- [x] A股 `score_diagnostics` 新增 `evidence_reason_summary`、`evidence_source_summary`、`missing_and_default_like_dimension_counts`、`evidence_coverage_distribution` 和全维缺证据样本 reason，用具体 reason 定位 SharedSignals/MarketGraph 哪条证据链缺失，不降低 `combined >= 0.55` 候选门槛。
- [x] `ashare_preopen_dry_run` 在候选池为空时同步输出上述 score diagnostics；`ashare_opening_validator` 会把 `missing_regime`、`missing_capital_flow_rows`、`insufficient_daily_bars` 等映射到 `check_marketgraph_all_weather_regime`、`check_sharedsignals_capital_flow`、`check_sharedsignals_daily_bar_history` 等可执行动作。
- [x] `ashare_preopen_dry_run` 的最新样本选择改为先限定普通 A股代码再取最新交易日；SharedSignals 同库内可转债/债券更新到更新日期时，不会再把普通股票候选池挤成 0。执行门禁价格会在 reader 缺失时回退 read model 最新收盘价；资金计划明确无新买预算时显示 warn 而非 fail，避免把“已有持仓/现金不足所以不新增买入”误报为执行故障。
- [x] CNFutures 将可执行合约判断提升到 `contract_rules.is_executable_contract_symbol`，adapter 与盘前验收共用同一过滤；盘前验收同时报告 raw/executable symbol、产品覆盖、5分钟 read model 可达性和风格状态。
- [x] CNFutures force-flatten 平仓按已有持仓 `avg_price` 和成交价计算 realized PnL；`score_records` 增加 `pnl_attribution`，避免“收益 0”被误读为真实无收益而不是样本不足或未闭合盈亏。

### 2026-07-07 A股健康检查 advisory 收口

- [x] `shared/runtime_test/market_health.py` 将“全部为 `outside_ashare_regular_session` 的 A股链路验证样本”从 warn 降为 pass/info advisory；样本仍在 details 中展示，并继续隔离出策略绩效/方向命中/自我演化口径。
- [x] A股开盘/盘中验收读取 `ashare_no_trade_explanations.jsonl` 时保留 `score_diagnostics`，并将 `candidate_pool_status`、`data_quality_status`、`max_combined`、候选阈值和 threshold counts 带入 `latest_no_trade_log` / `diagnostic_summary`；空池告警可直接区分策略阈值未过、研究维度中性或候选池分层异常，不改变交易阈值。
- [x] A股资金计划对账接入本地模拟样本质量：若资金计划早于 outside-session 链路验证样本快照且不一致，显示 pass/info advisory；若是真实策略样本后的资金计划滞后、缺计划、来源缺失、账本/快照不一致，仍保持 warn/fail。
- [x] A股失败/回执健康检查同步接入样本质量：纯 outside-session 链路验证样本不再要求策略成交回执，显示 pass/info advisory；失败订单、真实策略成交或缺来源样本仍保持 warn。
- [x] 新增/更新 `tests/test_market_health.py` 覆盖链路验证样本 advisory、资金计划 validation-only advisory 和回执 validation-only advisory，保留真实错配/缺来源 warn/fail 测试。
- [x] 生产侧已确认 SharedSignals A股资产入口通过 `/tushare?api_name=stock_basic` 恢复，TradingAgent reader 可读取 5000 条 A股资产样本；本次另发现 SharedSignals `/health` 在大 WAL/并发巡查期间仍可能慢响应，归入 SharedSignals 读侧运维优化，不作为 TradingAgent 策略成交故障。

### 2026-07-07 A股盘前 dry-run 验收

- [x] 新增 `shared/runtime_test/ashare_preopen_dry_run.py`：在开盘前只读预演 A股日线覆盖、最新高流动性普通 A 股小样本候选池评分、200,000 元模拟账户动态资金计划和执行门禁；默认样本上限 10 只，避免盘前检查全市场逐票扫描；只写 `shared/runtime_test/ashare_preopen_dry_run_latest.json` / history，不写 `signals/`、server-local ledger、pending、review 或实盘队列。
- [x] 新增 wrapper `shared/wrappers/job_ashare_preopen_dry_run.sh` 与 cron 模板 `35 8 * * 1-5`；wrapper 默认 90 秒超时、单次运行，异常时走系统邮件通道，正常通过不发邮件。
- [x] 新增 `tests/test_ashare_preopen_dry_run.py`，覆盖数据过期 fail、候选为空 warn 安全空跑、候选/资金/价格正常时生成只读 synthetic order 且带 `candidate_pool_layer=candidate` 与 `execution_source=ashare_candidate_layer`。

### 2026-07-07 A股/CNFutures 开盘稳定性修复

- [x] A股 `AshareAdapter` 的生产 `score_universe_limit` 从 200 扩到 500，覆盖到候选池 cap，避免只评分 universe 前几百只导致 candidate 层长期为 0；新买入仍必须来自 `candidate_pool_layer=candidate`，不放松候选阈值、不回退到 watch/universe 硬买。
- [x] A股 universe 已在过滤后按近期成交额/流动性做稳定预排序，避免 `score_universe_limit` 截断到代码顺序前段；候选池构建复用本轮 `score_universe` 预计算评分，不再对 candidate/watch 层逐票重算，避免候选分层、排序、资金计划和诊断口径漂移。
- [x] A股 `no_trade_explanation` 已补 `score_diagnostics`：候选为 0 时记录已评分数量、候选阈值、Top 分数、中性默认维度计数和缺失维度计数，后续可直接判断是 SharedSignals/MarketGraph 研究维度不足，还是候选确实没有达到交易门槛。
- [x] A股空池诊断新增 `candidate_above_threshold_count`、`watch_above_threshold_count`、`max_combined`、`candidate_pool_status` 和 `data_quality_status`，用于区分“策略阈值未过”“观察层有效但未达买入”“候选池分层异常”和“研究维度大面积中性”。
- [x] A股六维评分已补供数兼容：`TradingagentDataReader.get_factors()` canonical 查询 `Ashare` 与带后缀代码，`six_dimension_scorer` 在旧 factors 为空时读取 `get_fundamentals()` 与 `get_capital_flow()` 行并映射为 value/growth/quality/capital 分；资金分只混合金额字段并对专用 `/capital_flow` 行优先去重，避免把 `net_mf_vol` 当金额；候选池/轮动条件统一识别 `api_name:metric` 前缀；保留缺数据回退 0.5，不放松 candidate 阈值。
- [x] A股文档已明确候选池样本不能过小，扩大评分覆盖只解决“看得太少”的问题，不改变“无科学候选就留现金/逆回购”的资金门禁。
- [x] CNFutures 继续由 TradingAgent 只读 SharedSignals `market_bars_intraday`；Tushare 5 分钟期货接口空返回的备源修复在 SharedSignals 完成，TradingAgent 不新增独立采集。
- [x] CNFutures adapter 的 universe/合约发现已改为 `TradingagentDataReader` reader 优先，开盘验收也优先通过 reader 查询 Futures 日线/5分钟线；直接 SQLite 只保留为显式临时库或 `CN_FUTURES_ALLOW_DIRECT_SQLITE_FALLBACK=1` 的兼容兜底，不再是生产默认路径。
- [x] CNFutures 5 分钟 runner 已修正闭市口径：收盘后再次运行返回 `market_closed` 并写正常复盘行，不再把闭市后的最后一根有效 bar 误报为 stale；交易时段内真实 stale 仍保持拦截。
- [x] CNFutures 开盘/首样本验收已区分 5分钟数据缺失、首模拟样本缺失、策略主动 hold 和夜盘未授权风格不交易；该修复只读复盘 `hold_reason_summary`，不改变模拟成交策略。
- [x] CNFutures 开盘验收的 read-model SQLite 兜底放宽到 `5min`/`5m`/`5` interval 统一口径，不再锁死 `rt_fut_min` provider；reader 短缺时仍标记 `reader_shortfall`，TradingAgent 不新增独立采集。
- [x] PM/Crypto 健康检查已把空跑分成 `market_data_wait`、`strategy_wait`、`execution_fault`：缺行情是数据等待，PM 缺 MarketGraph 独立概率/edge 不足或 Crypto 动量阈值未过是策略等待，只有应成交却无账本才算执行故障。
- [x] 前端市场摘要新增 `runtimeState` / `executionFault`，市场切换后显示“运行中 / 策略等待 / 需要处理 / 等待数据”的结果层，避免把所有 warn 误读为系统坏；桌面和移动页面已做真实切换检查。CNFutures 看板摘要已接入 `shared/review/data/cn_futures_sim_reviews.jsonl`，有复盘 hold 样本时不再误显示为“等待数据”。
- [x] 2026-07-07 盘中已确认的生产症状：A股 job 正常运行但 `candidate_count=0`、无成交、权益约 200,000；CNFutures job 运行但缺 5 分钟 Futures bar、无成交。上述状态不能算“可交易健康”，必须等生产部署后复验 candidate、bar、filled/hold/no-trade 归因。

### 2026-07-05 simulated equity snapshot writer for dashboard

- [x] `shared/accounting/sim_ledger.py` 的 `daily_mark_to_market()` 已补齐前端收益看板所需字段：`capital_base`、`total_equity`、`total_pnl`、`realized_pnl`、`unrealized_pnl`、`return_pct`、`target_return_pct`、`max_drawdown_pct`、`trade_count`、`missing_mark_count` 和 `pnl_source`。
- [x] 新增 `shared/review/equity_snapshots.py` 与运维入口 `shared/runtime_test/write_equity_snapshots.py`，可扫描 `shared/logs/sim_ledger/<market>/<style>/positions.json`，读取 SharedSignals 价格后追加 `daily_mark_to_market.jsonl`；价格缺失时保守按成本估值并标记 `sim_ledger_cost_fallback`。
- [x] 该入口只写模拟账本权益快照，不写 `signals/`，不触发交易，不接触实盘队列、账户、邮件或 webhook；前端 `front/` 会把这些快照作为实时收益曲线的最高优先级来源。
- [x] 新增 `shared/wrappers/job_equity_snapshots.sh` 与 crontab 模板，每 5 分钟刷新一次模拟盘权益快照；wrapper 带 flock、独立日志和 `TRADINGAGENT_DASHBOARD_TARGET_RETURN_PCT` 目标收益配置。
- [x] `load_mark_prices_for_positions()` 已按市场读取最近可用价格：A股/美股/期货取最近日线，Crypto 取最新 ticker，PM 按 `market_id` 取预测市场最新价格；周末/节假日不再只查当天导致全部成本价兜底。若仍缺价，会继续保守标记 `sim_ledger_cost_fallback`。
- [x] `front/` 首页机会管道已改为事件优先的动态管道展示：机会进入、初筛、研究、风控、待执行和结果分流在同一面板内流动展示；该面板只读 `funnelEvents` / signals，不写队列、不触发执行。

### 2026-07-05 combined-cron health reporting

- [x] `cron/health_check.sh` 新增 `combined_crontab` 只读检查，调用 MarketGraph `deploy/install_combined_crontab.sh --check`；任一关键 cron 缺失会把 TradingAgent 外部健康报告标记为 `critical`。
- [x] 检查结果写入 SharedSignals `logs/watchdog_inputs/tradingagent_health.json(.jsonl)`，复用现有 watchdog 与系统邮件链路；不新增 daemon、不安装新 crontab、不修改交易队列。
- [x] SharedSignals API 探针增加 3 次短重试，避免 API 单次慢响应或 SQLite 瞬时锁竞争把整条健康链路误报为 `critical`；连续失败仍按 critical 上报。
- [x] 该检查只读执行，不会安装或覆盖 live crontab；真正安装仍只允许走 MarketGraph 合并安装脚本。

### 2026-07-05 cross-repo path and stale docs cleanup

- [x] TradingAgent 模拟回执默认写入 `signals/sim_execution_receipts.jsonl`；旧 `MarketGraph/outputs/sim_execution_receipts.jsonl` 只在历史文件存在时作为兼容读取，不再作为默认写入面。
- [x] `TradingagentDataReader` 不再默认读取同机 `/opt/investment/MarketGraph`；MarketGraph CSV fallback 必须显式设置 `MARKETGRAPH_DATA`，便于未来三系统分服务器独立运行。
- [x] A股 T+1 日历查找移除默认 MarketGraph 数据目录，改为 SharedSignals root / calendar root。
- [x] `shared/env_loader.sh` 不再 source MarketGraph deploy env，也不再把 MarketGraph 仓库加入 `PYTHONPATH`；TradingAgent env 优先，公共 finance env 仅作为兼容密钥来源。
- [x] TradingAgent cron 模板移除 MarketGraph 观察任务；MarketGraph 任务归属 `MarketGraph/deploy/crontab.txt`，日志写入 MarketGraphRuntime。
- [x] 删除 2026-06-30 过期 handoff 文档，重写 `docs/data_sources.md`，修正 `docs/AGENTS.md` 中过期/不存在入口。

### 2026-07-05 simulated matching residual risk fixes

- [x] 主服务器 live crontab 已恢复为 SharedSignals + TradingAgent 合并版本；CNFutures 单市场健康检查中 `cn_futures_cron` 从 fail 恢复为 pass。修复前缺失的是 SharedSignals `cn_futures_5min.sh` 采集调度，不是 TradingAgent 模拟执行器。
- [x] 当前 `market_health.py --market cn_futures` 生产结果为 warn/0 fail：cron 已齐，剩余 warn 是周末/闭市尚无 Futures 5分钟数据、模拟复盘样本和风格输出，等待下一交易时段产生样本。
- [x] `CNFutures/contract_rules.py` 与 `margin_model.py` 已将期货手续费从隐式数值判断升级为显式 `open_fee_type` / `close_fee_type`，支持 `rate` 与 `fixed_per_lot`，当前测试覆盖 `rb` 费率制和 `m` 固定每手制。
- [x] `shared/runtime_test/market_health.py --market cn_futures` 已接入 CNFutures 只读 live-chain validator，方便单市场排查 SharedSignals 5分钟数据、cron、模拟日志、复盘样本、风格输出和实盘关闭状态。
- [x] `front/` 看板信号漏斗新增 `stageEvidence`，成交账本回放标记为 `replay`；风险拒绝、触发、成交和部分阶段时间会在只读 snapshot 中体现，前端不会写入任何交易队列。

- [x] `SimExecutionEngine` 市场单边界检查改为按预估执行价判断，避免 dummy `limit_price` 误触发涨跌停/概率边界；新增轻量 `counterparty_profile` / liquidity / impact 参数用于 A股散户/对手盘环境模拟。
- [x] A股 server-local 执行器不再用默认 `ask_size=quantity` 覆盖 5分钟 `bar_volume`，无盘口量时会按 bar volume 与整手约束形成 partial fill。
- [x] A股 server-local 执行器会从本地模拟账本补齐 `cash_available` 与 T+1 `sellable_qty`，覆盖字符串 account 路径；同日卖出和现金不足已有集成测试。
- [x] `auto_pipeline` 已从 SharedSignals reader 的 5分钟/日线 bars 生成 `market_snapshot`，`StyleRunner` 会透传盘口、bar volume、previous_close、现金/可卖量和对手盘环境字段。
- [x] `auto_pipeline` 已兼容当前 `DecisionEngine` 旧接口，新增 all-stage smoke；A股基础 styles 已全部通过统一 `TradeStyle` 校验，`closing_momentum` 保持 paused。
- [x] `TradingagentDataReader.get_bars_intraday()` 已改为 SharedSignals API-first，通过 `/realtime_5min?market=...` 读取 A股/期货 5 分钟 read model；API 不可用或返回空壳时仍回退本机 SQLite。
- [x] `market_health` 对 A股/CNFutures 首样本状态加入交易时段判断：闭市等待不再形成系统 warn；交易时段应有样本但缺失时仍保持 warn，避免周末误报和开盘漏报。

### 2026-07-05 Ashare health bootstrap receipts

- [x] A股健康检查对 `signals/positions/simulated_ashare_positions.json` 增加 bootstrap 判断：服务器本地模拟盘尚无成交时，缺少持仓快照不再误报为异常；一旦出现本地模拟成交，仍要求快照可读。
- [x] `failure_receipts` 检查增加本地模拟成交计数：无失败、无本地模拟成交时视为“回执待首笔事件生成”；有失败或有成交时仍必须存在可读回执。
- [x] 新增回归测试覆盖无交易 bootstrap、失败无回执告警和影子盘状态推进，避免健康检查在周末/首日空样本时持续误报。

### 2026-07-05 style evolution runtime state isolation

- [x] `shared/markets/evolution_engine.py` 不再把演化后的 weight/status/paused/deprecated 写回 `Crypto/PM/US/HK/styles/*.json`。
- [x] 自动生成 variant 已迁到 `shared/review/<market>/generated_styles/`；`StyleRunner` 会合并读取基础 styles 与 runtime generated styles。
- [x] `shared/markets/performance_tracker.py` 移除 Python 版本不兼容的 `zip(..., strict=True)`，恢复本机/服务器兼容。
- [x] 新增 `tests/test_evolution_runtime_styles.py`，覆盖基础 styles 文件不变和 runtime variant 可被 StyleRunner 加载。

### 2026-07-04 CNFutures health/ops/dashboard data outlet

- [x] `CNFutures/review.py` 在追加 `cn_futures_sim_reviews.jsonl` 后，同步写入 `shared/review/cn_futures/style_comparison.json` 和 `style_performance.jsonl`，复用现有 metrics/style-comparison 数据出口，不新建单独看板。
- [x] 期货复盘新增 `error_summary` 与 `style_health`，按风格归类 `stale_intraday_bar`、`repeated_same_side_exposure` 等拒绝原因，并给出 simulated-only 的观察/降权建议。
- [x] `shared/runtime_test/market_health.py --market sim` 默认纳入 `cn_futures`，使用 CNFutures append-only review 作为模拟账本依据；真实样本未出现时按 warn 处理，不误报为生产故障。
- [x] `shared/runtime_test/ops_report.py` 新增 `cn_futures_review_summary`；`shared/review/metrics_dashboard.py` 可读取 `shared/review/cn_futures/style_performance.jsonl`，后续看板可直接接入。
- [x] 边界：本次只增加复盘、健康和看板数据出口，不写实盘队列、不接 CTP、不修改生产 crontab。

### 2026-07-04 email route residual fixes

- [x] `.env.example` 的 `CLOUDFLARE_EMAIL_FROM` 已从旧 `notice@agentspaces.cc` 改为 `notice@tradingagent.cc`。
- [x] `shared/notify/alert_router.py` 文档注释已同步为交易通道 `notice@tradingagent.cc -> tradingadviser@coze.email`。
- [x] simulated evolution circuit breaker 的系统告警不再硬发 `tradingadviser@coze.email`；`send_template_email()` 已改为显式 `channel` 优先解析默认收件人。

### 2026-07-04 system email smoke verification

- [x] 主服务器实测 TradingAgent 系统邮件：Cloudflare Email Service 从 `notice@tradingagent.cc` 发往 `soc@coze.email` 成功，主题含 `[SMOKE][TradingAgent][系统]`。
- [x] 邮件边界保持不变：交易类发 `tradingadviser@coze.email`，系统类发 `soc@coze.email`；`notice@agentspaces.cc` 不作为三系统交易发件邮箱。

### 2026-07-04 CNFutures 5-minute simulated trading cadence

- [x] `CNFutures/adapter.py` 已支持读取 SharedSignals `market_bars_intraday`，使用 `market="Futures"`、`interval="5min"` 作为期货 5 分钟模拟交易输入。
- [x] `CNFutures/adapter.py` 默认读取器已修正为 `TradingagentDataReader`，保证默认路径走 SharedSignals API-first；SQLite 直接读只保留为显式降级/测试回退。
- [x] `CNFutures/run_simulation.py` 默认 `--cadence 5min`；`CNFutures/sim_runner.py` 会优先读取分钟线，订单幂等键包含最新 `bar_time`，避免 5 分钟调度被同日幂等挡住。
- [x] 5 分钟 runner 已加入 `--max-intraday-bar-age-minutes` / `CN_FUTURES_MAX_INTRADAY_BAR_AGE_MINUTES`，默认最新 bar 超过 10 分钟则拒绝模拟下单并记录 `stale_intraday_bar`。
- [x] 同一交易日、同一风格、同一合约的连续同方向模拟信号会被标记为 `repeated_same_side_exposure`，避免每 5 分钟重复加同方向风险；反向信号仍允许形成新模拟成交。
- [x] `shared/wrappers/job_cn_futures_sim.sh` 显式以 `--cadence 5min` 运行，仍只写 simulated signal/review，不写实盘队列。
- [x] 生产 crontab 模板已改为期货日盘/夜盘每 5 分钟运行，并相对 SharedSignals 采集错后 1 分钟读取最新 bar。
- [x] 生产已确认 Tushare/QuickSync `rt_fut_min` 权限不足；SharedSignals 已启用 AKShare/Sina 5 分钟模拟盘备源并写入同一 `market_bars_intraday`，provider 为 `akshare_sina_rt_fut_min`。TradingAgent 继续只读 SharedSignals read model，不直接调用 AKShare/Tushare。

### 2026-07-04 CNFutures review scoring + fail-closed live reserve

- [x] `CNFutures/review.py` 新增 `score_records()`，复盘 JSONL 每轮追加 `score_summary`；open-only 或样本不足默认 `sample_insufficient`，不伪造收益能力。
- [x] 评分字段覆盖 `trade_count`、`filled_count`、`fee`、`margin_required`、`notional`、`realized_pnl`、`win_rate`、`max_drawdown`、`score` 和 `status`。
- [x] 新增 `CNFutures/live_gateway.py` 作为未来 CTP/SimNow/期货公司接入占位；当前 `real_trading_enabled=false`、`broker_adapter_ready=false`，所有真实期货订单请求抛 `SafetyViolation`，不得降级为 simulated。
- [x] `CNFutures/README.md` 已同步评分用途与实盘预留边界。
- [x] `shared/crontab.txt` 已补 CNFutures 模拟入口；SharedSignals 负责期货行情采集，TradingAgent 只读 SharedSignals read model 做 simulated 交易。

### 2026-07-04 SharedSignals-only data-source audit

- [x] 主服务器 `/opt/investment/tradingagent` 已确认：TradingAgent 生产模拟盘、影子盘、健康检查和研究路径不直接采集外部市场数据；市场数据入口是 SharedSignals API-first reader，SQLite read model 只作只读回退。
- [x] 服务器误拷贝的 untracked `Users/` 目录含 7 个过期文件，落后于当前 `shared/wrappers/tradings_cron_entry.py`、`shared/execution/local_sim_ledger.py`、A股节假日/T+1 修复等生产代码；已删除并在 `.gitignore` 中忽略 `Users/` 防止复发。
- [x] `crontab.txt` 与 `shared/crontab.txt` 已改为 2026-07-04 生产快照/边界说明；旧 2026-07-03 模板不再作为当前生产事实。
- [x] 服务器运行状态：A股本地模拟、Crypto、US、PM、健康检查、复盘、CNFutures 模拟入口均在 `marketgraph` 用户 crontab 中；HK 按 Nicholas 决策继续停用。

### 2026-07-04 server-side simulated trading closure

- [x] 后续调整：HK 按 Nicholas 最新决策暂不接入生产模拟盘，HK cron 已停用，默认模拟健康检查范围改为 A股/Crypto/PM/US；HK wrapper 和配置保留，未来可显式恢复。

- [x] A股模拟盘默认改为服务器本地 `server_local_sim_only` paper fill，Hermes/同花顺 GUI 路径保留为 `ASHARE_SIM_HERMES_ENABLED=1` 的第二选择；默认不写 `signals/pending`，不再因 Mini 不在线阻断服务器训练数据。
- [x] `job_ashare_sim_exec` 默认关闭 Hermes/webhook，只运行服务器本地模拟闭环；Hermes 启用后仍保留 mini health/backpressure 和回执保护。
- [x] `sim_broker` 支持在新进程中自动加载 A股/Crypto 内建 executor，并只对 `filled|partial|pending` 结果写本地模拟备份，避免 failed/rejected 污染账本。
- [x] 多市场 `StyleRunner` 已接统一 `shared/accounting/sim_ledger.py`，Crypto/PM/US/HK 的 filled/partial simulated 结果写入 `shared/logs/sim_ledger/<market>/<style>/`，重复订单按 `order_id` 幂等跳过。
- [x] 新增 `shared/review/sim_ledger_reader.py`，日报/周报/归因/汇总邮件已从旧 shadow-only 输入升级为 review 输入：legacy shadow fills + 统一 simulated ledger + A股 server-local simulated ledger；邮件和 JSON 结果新增 review/shadow/simulated/real 分层计数。
- [x] Crypto 模拟器在 SharedSignals 行情缺少可用价格时使用信号自带价格兜底，避免仅因行情接口空值丢失可训练样本。
- [x] HK 新增 `job_hk_sim.sh`，`run_sim.py` 已支持 HK；2026-07-05 按 Nicholas 最新决策改为默认 fail-closed，只有显式 `TRADINGAGENT_HK_SIM_ENABLED=1` 才能手动运行，`Global/HSI` 代理回退需额外 `SIM_HK_PROXY_ENABLED=1`。
- [x] `market_health.py --market sim` 新增多市场模拟健康检查，覆盖 cron、SharedSignals 数据、最新运行 JSON 和统一模拟账本；`job_sim_market_health.sh` 已加入 marketgraph crontab，每 10 分钟只读巡检；当前结果为 Crypto/PM/US pass，A股/HK warn，0 fail。
- [x] 验证：A股隔离执行确认不启用 Hermes 时可本地成交且不写 pending；手动运行 crypto/pm/us `run_sim.py` 返回 ok；HK 旧 HSI 代理成交仅作历史样本，当前默认 disabled；日报/周报新增模拟账本读取回归；目标测试与 `py_compile` 通过记录见本轮回执。

### 2026-07-04 A股 SharedSignals API universe + health fix

- [x] `TradingagentDataReader.get_assets()` 新增 A股 API-first 资产入口，通过 SharedSignals `/tushare?api_name=stock_basic` 读取 3781 条资产，健康检查识别 3480 条普通 A股。
- [x] `get_bars_daily()` / `get_market_data()` 修复单日查询参数：只有 end/date 时自动设置 start=end，避免 SharedSignals `/market_data` 返回空壳行。
- [x] A股健康检查改用生产同款 `TradingagentDataReader`，空影子账本用 `shadow_broker` 回放为 0 PnL；模拟持仓快照缺失从 fail 调整为 warn；脚本可直接运行并默认使用本机 SharedSignals API `127.0.0.1:8082`。
- [x] 补齐真实交易安全门日志路径所需 `logging` 导入，避免错误分支复盘日志触发 `NameError`。
- [x] 验证：`tests/test_data_reader.py tests/test_market_health.py` 12 passed；A股健康检查 6 pass / 2 warn / 0 fail；隔离闭环测试确认 Mini webhook 禁用时不写 Hermes `signals/pending`，仍可完成服务器本地 paper fill。

### 2026-07-04 P2 cleanup smoke coverage

- [x] `shared/orchestrator.py` 新增可选批量打分路径：默认依赖使用 `score_universe`，不可用时回退逐标的 `score_stock`。
- [x] `shared/wrappers/tradings_cron_entry.py` 移除 `_DEPRECATED` shadow job handler 注册，保留当前 `job_trading_signals`、sim 和日度入口。
- [x] 新增最小 smoke 测试覆盖 `shared/execution/sim_engine.py` 与 `shared/execution/auto_pipeline.py` 的 import/init/no-crash；新增 orchestrator 批量打分回归。
- [x] 验证：本地目标 `py_compile` 与 pytest 结果见本轮回执。

### 2026-07-04 P1 runtime hardening fixes

- [x] `shared/execution/execution_router.py` 路由历史读取已从整文件 `.readlines()` 改为 tail 读取最近固定行数，避免 JSONL 日志增长后一次性占用过多内存。
- [x] `shared/risk/pre_trade_check.py` 与 `shared/portfolio/exit_manager.py` 的 A 股 T+1 fallback 已补 2026 已知休市日；当 `Ashare.t_plus_1` 不可用时不再只跳周末。
- [x] 新增 `.github/workflows/test.yml` 与最小 `requirements.txt`，CI 覆盖 `compileall` 与 pytest。
- [x] 验证：本地目标 `py_compile` 与 pytest 结果见本轮回执。

### 2026-07-04 P0 resilience fixes

- [x] `position_ledger.py`、`capital_ledger.py`、`signal_state_machine.py`、`local_sim_ledger.py`、`shadow_broker.py` 的阻塞 `flock(LOCK_EX)` 已改为 `LOCK_EX | LOCK_NB`，并加 3 次递增等待重试；拿不到锁时显式 `TimeoutError`，不假成功。
- [x] `webhook_sender.py` 的 `time.sleep(0)` 已替换为指数退避，避免 Mini webhook 异常时忙等。
- [x] `shared/review/benchmark.py` 的 SQLite fallback 查询已移除列上的 `LOWER()` / `REPLACE()`，改为市场值枚举和日期格式范围查询，保留只读 fallback 行为。
- [x] 验证：目标 Python `py_compile` 通过；受影响 TradingAgent pytest 集合 54 项 + 6 subtests 通过；2026-07-07 已将 `WEBHOOK_SECRET` 空值告警改为仅在实际发送 Mini webhook 时触发，普通模拟盘/研究/健康检查导入路径不再刷生产日志。

### 2026-07-04 A股 API-first 与邮件通道对齐

- [x] `shared/env_loader.sh` 已默认注入 `SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`，A股 `job_ashare_sim_exec` 运行时通过 SharedSignals/ShareChannel API 优先取数。
- [x] Cloudflare 邮件凭据加载入口从不存在的 `/opt/investment/MarketGraph/.env` 改为 `/opt/marketgraph/.env`，并兼容 `CF_EMAIL_*` alias。
- [x] `/opt/investment/.env` 的旧邮件地址漂移已修正；交易通道和系统通道按 `AGENTS.md` 分流。

### 2026-07-04 测试状态泄漏修复

- [x] `tests/test_signal_state_machine.py`、`test_real_money_boundary.py`、`test_t_plus_1_integration.py` 中直接改写的模块级路径已改为 `patch.object` + cleanup，测试结束后自动恢复原值。
- [x] `tests/test_sim_loop.py` 已保存并恢复 `sim_executor_registry` 注册表，避免测试注册的 market executor 泄漏到后续用例。
- [x] 验证：`python3 -m pytest tests/test_signal_state_machine.py tests/test_real_money_boundary.py tests/test_t_plus_1_integration.py tests/test_sim_loop.py -q` 通过（23 passed，1 个既有 `WEBHOOK_SECRET` 空值 warning）。

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
- [x] 生产 crontab 已安装 `cron/auto_pipeline.sh`；Ashare 当前通过本地 simulated adapter 保持不触碰 Hermes/Mac Mini/同花顺，Hermes 仅作为显式启用的第二路径。

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
- [x] 生产 crontab 已安装 `cron/health_check.sh`；guard circuit breaker 已完成本地语法和既有演化回归验证，生产侧由统一健康 cron 上报。

### 2026-07-04 多市场多风格 simulated 自演化闭环

- [x] 新增 `shared/markets/performance_tracker.py`：`StylePerformance`、`style_performance.jsonl` 幂等写入、90 天历史加载、PnL 趋势回归和风格综合排序。
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
- [x] 生产 crontab 已安装多市场模拟与健康检查脚本；真实交易保持 fail-closed，多市场生产闭环继续按真实交易时段积累样本。

### SharedSignals API 15/15 端点迁移对齐（2026-07-03；2026-07-08 事件过滤补齐）

- [x] `SharedSignalsAPIClient` 已覆盖 15 个数据端点：trading day、market data、fundamentals、reference、macro、capital flow、events、sentiment、crypto、PM、associations、impacts、industry、realtime 5min、tushare。
- [x] `TradingagentDataReader` 已接入 API-first 访问核心读取路径；API 不可用时回退 SQLite 只读路径并打 degraded 状态。
- [x] `get_events()` 现在向 SharedSignals `/events` 透传 `market`、`symbol` 和 `subject_code`；API 返回空壳或空候选时会再检查 SQLite 只读 `market_events` fallback，避免把候选层为空误判为正式事件证据也为空。
- [x] TradingAgent 侧不再把 15/15 客户端视为孤儿代码；MarketGraph 当前生产采集边界已切到 SharedSignals-owned collectors，研究图谱读取仍保留只读文件/DB 兼容路径。

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
- [x] `.env.example` 的 `SHAREDSIGNALS_API_URL` 默认指向 `http://127.0.0.1:8082`；SQLite 只保留为本机只读降级路径，默认 DB 指向 `/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite`。
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

**TradingAgent 历史发现（CRITICAL/HIGH，当前状态见上方 2026-07-04/07-05 条目）：**
- **MarketGraphCSVReader 路径错误：** `intake` 路径缺少 `data/` 目录，`get_regime()` 路径错误 — 导致体制信号、事件候选、情绪信号三个关键 CSV 静默加载失败（已修复）
- **SharedSignalsAPIClient 孤儿代码：** 已修复；`TradingagentDataReader` 默认 API-first，SQLite 只保留只读回退
- **TradingagentDataReader 无数据新鲜度检查：** 已补健康检查、错误告警和市场 loop 巡检
- **N+1 查询扇出：** 评分管线对每只股票做 5-6 次独立查询，20 只股票 > 100 次调用，无批量接口
- **直接 SQLite 读取绕过了 API 鉴权：** 已修复为 SharedSignals API-first，SQLite 仅为生产本机只读降级路径
- **无死人手刹：** 已补连续错误日志告警

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
