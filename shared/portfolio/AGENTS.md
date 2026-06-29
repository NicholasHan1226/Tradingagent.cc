# portfolio/

## 目标
组合构建 + 仓位分配 + 再平衡 + 退出管理。

## 文件
- `constructor.py` — 组合构建: risk_parity / equal_weight / conviction_weighted 三种方法。
- `position_sizer.py` — 仓位分配: size = belief_score × volatility_factor × regime_weight。
- `rebalancer.py` — 再平衡: regime 变化或相关性偏移时触发。
- `exit_manager.py` — 退出管理: 止损/止盈/时间退出/逻辑证伪。

## 原则
- 仓位 = 信心 × 波动率调整 × regime 倾斜
- 再平衡基于 regime 变化, 非定期
- 退出优先级: 止损 > 逻辑证伪 > 时间退出 > 止盈
- 组合构建默认 conviction_weighted

## 接口
```python
from portfolio.constructor import construct
from portfolio.position_sizer import size_position
from portfolio.rebalancer import check_rebalance
from portfolio.exit_manager import check_stop_loss, check_take_profit, check_time_exit, check_logic_invalidation
```

## 依赖
- adversarial 模块 (belief_score)
- risk 模块 (risk_limits)
