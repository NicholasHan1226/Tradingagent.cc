# screening/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
六维加权打分，不设硬门禁。维度：宏观/事件/基本面/资金/技术/情绪。主动发现机会。
TradingAgent 自身负责短周期机会发现；SharedSignals API/read model 是基础数据入口，覆盖宏观、事件、基本面、资金、技术行情和情绪；MarketGraph 只作为宏观、事件、情绪和中长线图谱研究补充，不作为 A股/CNFutures 的执行信号入口。

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
- 六维评分必须 SS-first、MG-enhanced：基础评分先消费 SharedSignals API；MarketGraph 缺失时只能记录 evidence debt 或中性降级，不得阻断 TradingAgent 自有交易闭环。
- A股个股事件维度通过 SharedSignals `/events` 读取最近 3 个自然日；显式方向/置信度优先，标题或正文关键词推断只使用固定 0.30 审慎置信度，不能随着 `min_confidence` 门槛提高而自动抬高。无方向公告保留为原始事件计数但不算催化证据；诊断必须区分无事件、无方向和低置信度。
- A股宏观维度必须能消费 SharedSignals 原始宏观因子：PMI 等 `factor_name/value` 行可转成市场级 macro 分数；不得因为没有 MarketGraph regime 就把 macro 全量标为缺失。
- A股 sentiment 可消费 SharedSignals 市场新闻流作为弱市场情绪，但只有明确利好/利空关键词或显式方向字段时才计入；无个股代码的新闻不能伪造成个股 event 催化。
- 当前 candidate_pool 是动态重建池，不是持久化状态机；没有落地 demote/退出、层内停留时间、复盘驱动迁移前，不得声称每层独立升降级闭环已完成。
- A股可执行 universe 必须同时满足普通 A股代码段、非 ST/非停牌/非新股、近期日线 close > 0 和流动性要求；无日线覆盖不得用默认价格补位。
- `candidate_pool` 若接收预计算 scores，必须直接按该评分分层并保留 candidate/watch 阈值，不得再次逐票重算造成候选层、排序、复盘诊断口径不一致。
- `candidate_pool` 的 fundamental 层是低频观察池；A股 5 分钟执行链、盘前 dry-run 等已传入预计算 scores 的高频入口默认不得同步全量加载 fundamental 池，避免低频观察池拖慢 candidate/执行验收。需要长期基本面观察时由低频研究入口显式启用。
- A股 `candidate` 层不能只靠技术/资金维度和缺失维度的 0.5 中性默认分穿过 `combined >= 0.55`；当六维评分带有证据元数据时，candidate 还必须满足最低证据覆盖，并且 event/fundamental/sentiment 至少一个研究维度有真实证据。不满足但分数较高的标的只能留在 `watch`，不得进入可执行 candidate。
- 当某一维度在同一轮全部评分标的中都缺证据时，`score_universe` 必须对整批统一移除该维度权重并记录 `batch_inactive_dimensions` / `batch_evidence_availability`；不得让缺证据的中性默认分参与排名，也不得按个股缺失情况重加权。
- A股 no-trade、盘前和看板诊断必须透出上述批次字段；看到 event/sentiment 为 0.5 时，应能确认它是被批次降权后的中性展示，而不是仍参与 `combined` 的伪证据。
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
- SharedSignals / ShareChannel API (assets, bars, factors, moneyflow, events, sentiment)
- MarketGraph API (regime/event/news/impact) 为可选增强；缺失时必须记录 evidence debt 或回到中性/安全空跑，不得绕过 candidate/执行门禁。
- Ashare data (moneyflow/scores/signals/forecasts)
