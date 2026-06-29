# screening/

## 目标
Six-dimension weighted scoring. No hard gates. Dimensions: macro/event/fundamental/capital/technical/sentiment. Proactive discovery.

## 文件
- `six_dimension_scorer.py` — 六维打分: macro/event/fundamental/capital/technical/sentiment → combined score。
- `universe_filter.py` — 全市场过滤: 排除 ST/停牌/新股/流动性不足。
- `candidate_pool.py` — 5层候选池: holdings→watch→candidate→universe→fundamental。
- `condition_generator.py` — 条件生成: breakout/pullback/event/value/rotation 五类条件。
- `condition_monitor.py` — 条件监控: 盘中5min K线检查条件触发。
- `weights.yaml` — 六维权重配置。
- `patrol.py` — 巡检: 因子衰减/分布偏斜/偏差检查。

## 原则
- 权重式打分, 不设硬门禁
- 降权不硬拒, 主动发现机会
- 条件驱动, 而非实时全量扫描
- 六维互补: 宏观定方向, 事件找催化, 基本面定底, 资金确认, 技术择时, 情绪防雷

## 接口
```python
from screening.six_dimension_scorer import score_stock, score_universe
from screening.universe_filter import filter_universe
from screening.candidate_pool import build_pool, get_layer
from screening.condition_generator import generate_conditions
from screening.condition_monitor import check_conditions
from screening.patrol import patrol
```

## 依赖
- weights.yaml (本目录)
- MarketGraph MCP (regime/event/news/impact)
- Ashare data (moneyflow/scores/signals/forecasts)
