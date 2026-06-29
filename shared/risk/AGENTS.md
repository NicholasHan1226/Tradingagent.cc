# risk/

## 目标
事前风控 + 持仓监控 + 黑天鹅应急。降权不硬拒, 但有硬底线 (单股 <15%)。

## 文件
- `pre_trade_check.py` — 事前风控: 仓位/相关性/板块/流动性检查。降权处理, 仅单股>15%硬拒。
- `position_monitor.py` — 持仓监控: 止损/回撤/时间退出/regime 变化。
- `black_swan.py` — 黑天鹅应急: 大盘 -3% / 重大政策 / 流动性危机 → 强制减仓。
- `risk_limits.yaml` — 风控参数: 单股/板块/总敞口/日亏/持仓数限制。
- `patrol.py` — 巡检: 定时扫描持仓风险状态, 输出告警。
- `heal.py` — 自愈: 自动修复常见风险违规 (超限减仓/相关性过高调权)。

## 原则
- 降权不硬拒, 但有硬底线
- 单股 max 15% (硬限, 超过即拒)
- 板块 max 40% (软限, 超过降权)
- 总敞口 max 80% (软限, 超过按比例缩)
- 日亏 3% → 暂停新增
- 黑天鹅 → 强制减仓至 50% 以下

## 接口
```python
from risk.pre_trade_check import check
from risk.position_monitor import check_positions
from risk.black_swan import check_black_swan
from risk.patrol import patrol
from risk.heal import heal
```

## 依赖
- risk_limits.yaml (本目录)
- portfolio 模块 (持仓数据)
