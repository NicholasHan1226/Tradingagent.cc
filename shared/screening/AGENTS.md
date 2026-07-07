# screening/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
六维加权打分，不设硬门禁。维度：宏观/事件/基本面/资金/技术/情绪。主动发现机会。

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
- 当前 candidate_pool 是动态重建池，不是持久化状态机；没有落地 demote/退出、层内停留时间、复盘驱动迁移前，不得声称每层独立升降级闭环已完成。
- A股可执行 universe 必须同时满足普通 A股代码段、非 ST/非停牌/非新股、近期日线 close > 0 和流动性要求；无日线覆盖不得用默认价格补位。
- `candidate_pool` 若接收预计算 scores，必须直接按该评分分层并保留 candidate/watch 阈值，不得再次逐票重算造成候选层、排序、复盘诊断口径不一致。
- `candidate_pool` 的 fundamental 层是低频观察池；A股 5 分钟执行链、盘前 dry-run 等已传入预计算 scores 的高频入口默认不得同步全量加载 fundamental 池，避免低频观察池拖慢 candidate/执行验收。需要长期基本面观察时由低频研究入口显式启用。
- A股 `candidate` 层不能只靠技术/资金维度和缺失维度的 0.5 中性默认分穿过 `combined >= 0.55`；当六维评分带有证据元数据时，candidate 还必须满足最低证据覆盖，并且 event/fundamental/sentiment 至少一个研究维度有真实证据。不满足但分数较高的标的只能留在 `watch`，不得进入可执行 candidate。
- A股盘前 dry-run、模拟主循环和开盘验收必须复用同一套 `candidate_pool.build_pool` 分层口径；不得在验收脚本里自行按 `combined >= 0.55` 拼 candidate。

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
