# TradingAgent / shared/markets

> 阅读顺序：[../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件。

## 边界

- 本目录只保留跨市场机械辅助和三条现役 lane 的显式注册；市场专属撮合、会话、保证金、T+1、价格限制与未来 broker adapter 分别归 `Ashare/`、`CNFutures/`、`Crypto/`。
- 现役 runtime market 精确为 `ashare`、`cn_futures`、`crypto`，由 `shared/governance/market_lanes.py` fail-closed 规范化。空值、未知值和 US/PM/HK 等退役市场不得继承 A 股规则、资本或执行默认值。
- `sim_capital.py` 只返回各市场原生币种的独立模拟基线，不负责 FX 转换，也不得用于跨市场资本汇总。
- 旧 `market_rules.py`、StyleRunner、StyleConfig、PerformanceTracker 与通用 EvolutionEngine 已物理删除；恢复能力仅来自 Git 历史，不保留可执行兼容入口。
- 当前演化 authority 仅为 SampleJournal/KPI 与 `shared/models/evolution_loop.py` 的 negative-only、人工复核生命周期；自动晋级、自动扩风险和 live transition 均关闭。

## 验收

- 未知或退役 market 输入必须显式失败。
- All Markets 仅汇总非货币计数；资本、权益、PnL、收益率和回撤始终按市场独立展示。
- 删除旧路径后同步更新 `legacy_inventory.yaml`、状态矩阵、退役静态门禁与运维文档，避免后续任务恢复旧 authority。
