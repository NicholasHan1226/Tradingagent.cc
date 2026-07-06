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
- 分批/旧持仓按唯一标的计入持仓数量；同一股票多条 lot 不得被误判为多只股票。
- 盘前1小时资金规划
- 闲置资金尾盘买逆回购(204001)

## 执行
- 模拟盘: 默认由服务器通过 `Ashare/sim_executor.py` 和 `shared/execution/sim_broker.py` 完成本地 paper fill、账本和复盘闭环；不依赖 Mini/Hermes。
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
