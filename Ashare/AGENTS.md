# tradingagent/Ashare

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
A股模拟交易全闭环：服务器本地模拟盘优先，保留 T+1、交易时段和普通 A 股过滤；Hermes/同花顺 GUI 执行仅作为未来可显式开启的第二路径。

## 约束
- T+1: 当天买不能当天卖
- 集合竞价: 9:15-9:25 (单独策略)
- 连续竞价: 9:30-11:30, 13:00-14:57 (主策略)
- 收盘竞价: 14:57-15:00 (单独策略)
- 涨跌停: 10%限制

## 资金
- 主模拟盘初始 200,000 元；`capital_plan.py` 按候选质量、风控拒绝率、数据异常率和近期表现动态决定 0/1/2/3 只。强信号可集中 2-3 只，弱信号或高拒绝率时优先留现金/逆回购，不为凑仓位硬买。
- 50,000 / 100,000 元不是普通配置值，而是资金档位实验账户。`Ashare/tier_experiments.py` 读取主账户策略有效成交，按各自本金、现金、100 股手数和手续费重放，写入 `shared/logs/local_sim_tiers/ashare_50000/`、`shared/logs/local_sim_tiers/ashare_100000/` 独立账本；`portfolio_evolution.py` 会把这些账户纳入组合级演化 `rankings`。
- 动态现金缓冲（替代原固定 30%）：激进约 17.5%，均衡 25% 且不超过 50,000，谨慎 45%，防守（弱候选/高风险/无候选/强制防守）全现金。均衡模式硬上限 50,000 避免 200,000 账户因百分比锁死 60,000。
- 样本收集模式：仅当累计策略样本低于 `min_strategy_samples`，且数据异常率、风控拒绝率和近期胜率都未触发防守时，`capital_plan.py` 可以开放 1 笔 20,000-35,000 元受控探索仓；不得因为当天尚无成交而创建买入容量。弱候选、高风险或数据异常仍保持防守/谨慎。`Ashare/evolution_controller.py` 只能输出 simulated-only 观察/风控上下文，不得下单、不得启用实盘、不得绕过 candidate 层、现金/手数、T+1、交易时段和风控门禁。
- 日内样本监控：`Ashare/sample_target_monitor.py` 在 09:45、11:45、14:30、15:30 记录候选与成交证据；当天没有成交只标记 `observation_gap` 并建议 `observe_and_label_candidates`，不失败、不刷新强制交易决策、不写订单。前向标签和候选决策证据用于学习，不能用成交配额替代策略判断。
- 资金计划使用“策略有效样本账户视图”：链路验证样本、非连续竞价样本、缺候选来源或完全缺成交价的样本仍保留为账户事实和复盘证据，但不得占用 `capital_plan` 的策略现金、目标持仓数、新买入容量或机会成本换仓判断。常规交易时段内来自 `candidate` 层、`execution_source=ashare_candidate_layer` 且有正成交价的 server-local 策略成交必须占用策略资金；模拟主循环应优先把 SharedSignals 最新有效 5 分钟 bar 附到订单。只有 `execution_evidence_class=verified_5min_market_data` 且带市场报价、bar time、bar volume 的成交可进入组合演化或风险扩张证据；其他价格来源只作较弱账本事实。
- 账本资金门禁是最后防线：A股 server-local 模拟买入写账前必须按订单所属的逻辑资金范围完整回放；`strategy` 与 `validation` 各自独立执行 200,000 元本金、负现金和可卖持仓硬门禁。链路验证样本必须写 `capital_scope=validation` 并保留在同一 append-only 审计文件，但不得占用或放松 `strategy` 账户的现金和持仓；上游资金计划不得依赖过期账户快照放行。
- 候选池打分样本不得过小；生产 `score_universe_limit` 应覆盖足够多的流动性过滤后股票，避免只看 universe 前几十只导致科学候选为 0。扩大样本不等于放松 candidate 阈值，未进入 candidate 层仍不得买入。
- 候选池评分样本必须先按近期流动性和数据完整性预排序；候选为 0 不自动等于系统故障，必须结合 `scored_count`、`top_scores`、维度中性默认/缺失计数判断是样本覆盖、研究供数还是策略阈值问题。
- 分批/旧持仓按唯一标的计入持仓数量；同一股票多条 lot 不得被误判为多只股票。
- 盘前1小时资金规划
- 闲置资金尾盘买逆回购(204001)

## 执行
- 模拟盘: 默认由服务器通过 `Ashare/sim_executor.py` 和 `shared/execution/sim_broker.py` 完成本地 paper fill、账本和复盘闭环；不依赖 Mini/Hermes。
- A股信号卡的 `t_plus_1.sellable_from/sellable_date` 必须写下一交易日；当天买入不得在同日进入可卖数量或换仓释放资金。
- A股 simulated 调度入口必须是 `shared.wrappers.tradings_cron_entry --job job_ashare_sim_exec`；通用 legacy `shared/wrappers/run_sim.py` 不承载 A股 no-trade 三段证据链，已显式拒绝 A股调用。
- 交易时段硬门禁: `Ashare/sim_executor.py` 自身会按 `Asia/Shanghai` 和 A股交易日历拒绝非连续竞价时段（09:30-11:30、13:00-14:57）的 server-local fill 与 Hermes pending；wrapper 只是第一层保护。`bypass_market_hours` / `mock_filled` 只能用于测试、回测或隔离烟测，不得用于生产模拟调度。
- 盘前 dry-run: `shared/runtime_test/ashare_preopen_dry_run.py` 只读预演日线覆盖、最新高流动性普通 A 股小样本的候选池、资金计划和执行门禁；默认样本上限 50 只，wrapper 默认 90 秒超时，报告必须记录实际 `score_limit`，避免 10 只小样本或版本/配置漂移误导看板；只允许写 runtime_test 最新/历史报告，不得写 `signals/`、账本、pending、review 或实盘队列。
- 盘前 dry-run 报告必须输出各段耗时，用于区分 SharedSignals 数据、候选池评分、资金计划和执行门禁哪一段拖慢开盘前检查。
- 样本隔离: 已发生的非连续竞价时段 A股 simulated 成交保留为账户事实和链路验证样本，但必须归类为 `outside_ashare_regular_session`，不得进入策略胜率、方向命中、策略 PnL 或自我演化样本。
- 活跃账户视图: A股 server-local 模拟盘默认读取 `strategy_samples_only`，只把交易时段内、来自 `candidate` 层、带成交价来源的策略样本计入现金、持仓、市值和收益；链路验证/盘外/缺来源样本仅作为 audit 视图展示，不得影响资金计划、目标持仓、机会成本换仓和看板累计收益。
- 健康检查口径: 单纯已归类的 `outside_ashare_regular_session` 链路验证样本不视为运行故障，应作为 pass/info advisory 展示；这类样本不要求策略成交回执，但真实策略成交、失败订单、缺来源字段、非普通 A股代码、账本/快照不一致、真实策略样本后的资金计划滞后仍必须 warn/fail。
- 卖出/换仓: `shared/orchestrator.py` 会在 A股模拟主循环中生成 simulated sell 压缩单；只卖可卖数量，优先处理止损、低分、超目标持仓压缩和轻量机会成本换仓，不触碰实盘。止损/压缩/机会成本换仓释放的资金可作为同轮替换买入预算，并写入 `capital_plan.replacement_budget`。机会成本只比较已通过风控候选与现有持仓的 `combined` 分数，默认候选分数至少 0.70 且分差至少 0.12 才触发，避免小分差频繁换仓，同时减少明显强候选被过度保守门禁错过。`shared/orchestrator.py` 进一步依据近期 forward validation 胜率、样本质量（`shared/review/sample_quality.py`）和现有持仓平均分数动态加宽机会成本分差（ hard floor 保持 0.12，仅可 widening/pause，不可收窄），并将 `dynamic_thresholds` 写入换仓计划日志供复盘。
- 成交后资金刷新: A股模拟主循环若出现 server-local filled，会追加一条 `refresh_phase=post_execution` 的资金计划日志，重新记录策略有效现金、持仓数和样本隔离状态；该刷新只用于复盘/看板证明成交后账户状态，不生成新订单、不改变同轮交易。
- Hermes 备用路径: 只有显式设置 `ASHARE_SIM_HERMES_ENABLED=1` 时，服务器才把模拟信号卡投递给 Mac Mini live executor `~/.hermes/scripts/sim-signal-executor.py`，由同花顺模拟盘执行并回写。
- 实盘: 仅人工确认与只读同步；不得自动点击真实账户委托
- 5-10分钟级别自动化

## 研究证据
- `research_evidence.py` 是 A股集合竞价、尾盘动能、204001 逆回购收益估算和风格证据的只读入口；输出到 `shared/review/ashare/`，不得写入 `signals/pending`、`signals/real` 或任何执行队列。
- 集合竞价证据优先使用 09:15-09:25 数据；缺失时只能用 09:30 首个 5 分钟窗口作 `first_5m_proxy` 研究代理，不得伪装成真实竞价撮合数据。
- 逆回购 204001 估算优先读取 SharedSignals reader 日线价格/收益率，缺失时才回退环境变量或默认值，并必须保留 `yield_source`。
- 风格预算优先读取 `shared/review/ashare/style_weights.json` 运行时权重，基础 `Ashare/styles/*.json` 只作配置兜底；paused/deprecated 风格不分配 200,000 元虚拟训练预算。
- `closing_momentum` 保持 research/paused，只有尾盘候选扫描、次日 open/high 兑现回测和样本阈值达标后，才能讨论进入 simulated。
- `forward_validation.py` 是 A股 server-local 策略成交的只读前向标签入口；只给策略有效成交标注 30/60 分钟、当日收盘、次交易日 open/high/close，不写执行队列，不改资金计划，链路验证/盘外/缺来源样本必须跳过。生产入口为 `shared/wrappers/job_ashare_forward_validation.sh`，只刷新 `shared/review/ashare/forward_validation_latest.json` 与历史验证文件，供复盘和看板读取。
- `portfolio_evolution.py` 是 A股组合级自我演化证据入口；读取 server-local 策略成交、资金档位实验账本、样本质量和盯市 PnL，写 `shared/review/ashare/portfolio_evolution_latest.json` 与 `portfolio_evolution_log.jsonl`。只有带市场报价、bar time、bar volume 的成交可进入演化证据；风险扩张还需要足够的已实现回合与 60 分钟前向标签。写入生产复盘前会用同一批 SharedSignals 盯市价刷新 `local_sim_pnl.json` 与持仓快照，成交事实仍只保存在 append-only `local_sim_trades.jsonl`。
- `evolution_controller.py` 将组合样本、强执行证据、已实现回合、前向标签和收益转成 `evolution_decision_latest.json`；证据不足时只能 `observe_and_label_candidates`，不能因浮盈或当日零成交而扩大风险或强制探索。它不写订单、不启用实盘、不直接修改成交事实。
- `sample_target_monitor.py` 是 A股盘中观察验收入口；读取组合演化、演化决策和 no-trade 解释，输出成交/候选证据状态。生产入口为 `shared/wrappers/job_ashare_sample_target_monitor.sh`，只写 review 证据，不写执行队列或成交事实。
- `sample_learning.py` 是 A股收盘学习报告入口；读取策略成交、forward validation、样本目标监控、no-trade 解释和三账户实验，写 `sample_learning_latest.json` / log。它负责样本质量分层、交易假设 ID 汇总、收盘 blocker 归因、动态探索仓建议、三账户目标拆分和因子研究状态；只读/只写 review，不写订单、不改账本、不启用实盘。当前 A股已有六维评分/因子消费，但因子研究必须以 `sample_learning.factor_research` 的样本数、forward return 和稳定性为准；样本不足时只能标记 `sample_debt`，不得把评分因子当成已验证 alpha。

## 现有代码
- 当前 A-share 代码位于本目录：`adapter.py`、`capital_plan.py`、`evolution_controller.py`、`sample_target_monitor.py`、`sample_learning.py`、`portfolio_evolution.py`、`research_evidence.py`、`sim_executor.py`、`t_plus_1.py` 和 `market_phases/`。
- 旧 `/opt/investment/Ashare/tools/a_share_*.py` 已退役/归档，不得作为新的执行或依赖入口。
