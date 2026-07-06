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
- 模拟盘初始 200,000 元, 集中 2-3 只
- 盘前1小时资金规划
- 闲置资金尾盘买逆回购(204001)

## 执行
- 模拟盘: 默认由服务器通过 `Ashare/sim_executor.py` 和 `shared/execution/sim_broker.py` 完成本地 paper fill、账本和复盘闭环；不依赖 Mini/Hermes。
- Hermes 备用路径: 只有显式设置 `ASHARE_SIM_HERMES_ENABLED=1` 时，服务器才把模拟信号卡投递给 Mac Mini live executor `~/.hermes/scripts/sim-signal-executor.py`，由同花顺模拟盘执行并回写。
- 实盘: 仅人工确认与只读同步；不得自动点击真实账户委托
- 5-10分钟级别自动化

## 研究证据
- `research_evidence.py` 是 A股集合竞价、尾盘动能、204001 逆回购收益估算和风格证据的只读入口；输出到 `shared/review/ashare/`，不得写入 `signals/pending`、`signals/real` 或任何执行队列。
- `closing_momentum` 保持 research/paused，只有尾盘候选扫描、次日 open/high 兑现回测和样本阈值达标后，才能讨论进入 simulated。

## 现有代码
- 当前 A-share 代码位于本目录：`adapter.py`、`capital_plan.py`、`research_evidence.py`、`sim_executor.py`、`t_plus_1.py` 和 `market_phases/`。
- 旧 `/opt/investment/Ashare/tools/a_share_*.py` 已退役/归档，不得作为新的执行或依赖入口。
