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
- 模拟盘初始 200,000 元；`capital_plan.py` 按候选质量、风控拒绝率、数据异常率和近期表现动态决定 0/1/2/3 只。强信号可集中 2-3 只，弱信号或高拒绝率时优先留现金/逆回购，不为凑仓位硬买。
- 动态现金缓冲（替代原固定 30%）：激进约 17.5%，均衡 25% 且不超过 50,000，谨慎 45%，防守（弱候选/高风险/无候选/强制防守）全现金。均衡模式硬上限 50,000 避免 200,000 账户因百分比锁死 60,000。
- 资金计划使用“策略有效样本账户视图”：链路验证样本、非连续竞价样本、缺候选来源或缺成交价来源样本仍保留为账户事实和复盘证据，但不得占用 `capital_plan` 的策略现金、目标持仓数、新买入容量或机会成本换仓判断。
- 候选池打分样本不得过小；生产 `score_universe_limit` 应覆盖足够多的流动性过滤后股票，避免只看 universe 前几十只导致科学候选为 0。扩大样本不等于放松 candidate 阈值，未进入 candidate 层仍不得买入。
- 候选池评分样本必须先按近期流动性和数据完整性预排序；候选为 0 不自动等于系统故障，必须结合 `scored_count`、`top_scores`、维度中性默认/缺失计数判断是样本覆盖、研究供数还是策略阈值问题。
- 分批/旧持仓按唯一标的计入持仓数量；同一股票多条 lot 不得被误判为多只股票。
- 盘前1小时资金规划
- 闲置资金尾盘买逆回购(204001)

## 执行
- 模拟盘: 默认由服务器通过 `Ashare/sim_executor.py` 和 `shared/execution/sim_broker.py` 完成本地 paper fill、账本和复盘闭环；不依赖 Mini/Hermes。
- A股 simulated 调度入口必须是 `shared.wrappers.tradings_cron_entry --job job_ashare_sim_exec`；通用 legacy `shared/wrappers/run_sim.py` 不承载 A股 no-trade 三段证据链，已显式拒绝 A股调用。
- 交易时段硬门禁: `Ashare/sim_executor.py` 自身会按 `Asia/Shanghai` 和 A股交易日历拒绝非连续竞价时段（09:30-11:30、13:00-14:57）的 server-local fill 与 Hermes pending；wrapper 只是第一层保护。`bypass_market_hours` / `mock_filled` 只能用于测试、回测或隔离烟测，不得用于生产模拟调度。
- 盘前 dry-run: `shared/runtime_test/ashare_preopen_dry_run.py` 只读预演日线覆盖、最新高流动性普通 A 股小样本的候选池、资金计划和执行门禁；默认样本上限 10 只，wrapper 默认 90 秒超时，避免盘前检查全市场逐票扫描；只允许写 runtime_test 最新/历史报告，不得写 `signals/`、账本、pending、review 或实盘队列。
- 盘前 dry-run 报告必须输出各段耗时，用于区分 SharedSignals 数据、候选池评分、资金计划和执行门禁哪一段拖慢开盘前检查。
- 样本隔离: 已发生的非连续竞价时段 A股 simulated 成交保留为账户事实和链路验证样本，但必须归类为 `outside_ashare_regular_session`，不得进入策略胜率、方向命中、策略 PnL 或自我演化样本。
- 健康检查口径: 单纯已归类的 `outside_ashare_regular_session` 链路验证样本不视为运行故障，应作为 pass/info advisory 展示；这类样本不要求策略成交回执，但真实策略成交、失败订单、缺来源字段、非普通 A股代码、账本/快照不一致、真实策略样本后的资金计划滞后仍必须 warn/fail。
- 卖出/换仓: `shared/orchestrator.py` 会在 A股模拟主循环中生成 simulated sell 压缩单；只卖可卖数量，优先处理止损、低分、超目标持仓压缩和轻量机会成本换仓，不触碰实盘。止损/压缩/机会成本换仓释放的资金可作为同轮替换买入预算，并写入 `capital_plan.replacement_budget`。机会成本只比较已通过风控候选与现有持仓的 `combined` 分数，默认候选分数至少 0.70 且分差至少 0.18 才触发，避免小分差频繁换仓。
- Hermes 备用路径: 只有显式设置 `ASHARE_SIM_HERMES_ENABLED=1` 时，服务器才把模拟信号卡投递给 Mac Mini live executor `~/.hermes/scripts/sim-signal-executor.py`，由同花顺模拟盘执行并回写。
- 实盘: 仅人工确认与只读同步；不得自动点击真实账户委托
- 5-10分钟级别自动化

## 研究证据
- `research_evidence.py` 是 A股集合竞价、尾盘动能、204001 逆回购收益估算和风格证据的只读入口；输出到 `shared/review/ashare/`，不得写入 `signals/pending`、`signals/real` 或任何执行队列。
- 集合竞价证据优先使用 09:15-09:25 数据；缺失时只能用 09:30 首个 5 分钟窗口作 `first_5m_proxy` 研究代理，不得伪装成真实竞价撮合数据。
- 逆回购 204001 估算优先读取 SharedSignals reader 日线价格/收益率，缺失时才回退环境变量或默认值，并必须保留 `yield_source`。
- 风格预算优先读取 `shared/review/ashare/style_weights.json` 运行时权重，基础 `Ashare/styles/*.json` 只作配置兜底；paused/deprecated 风格不分配 200,000 元虚拟训练预算。
- `closing_momentum` 保持 research/paused，只有尾盘候选扫描、次日 open/high 兑现回测和样本阈值达标后，才能讨论进入 simulated。

## 现有代码
- 当前 A-share 代码位于本目录：`adapter.py`、`capital_plan.py`、`research_evidence.py`、`sim_executor.py`、`t_plus_1.py` 和 `market_phases/`。
- 旧 `/opt/investment/Ashare/tools/a_share_*.py` 已退役/归档，不得作为新的执行或依赖入口。
