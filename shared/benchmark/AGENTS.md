# benchmark/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
基准跟踪与业绩比较。

## 当前分类与 ownership

- `benchmark_tracker.py` 是 TradingAgent 所有的 **active-compatibility、只读分析**模块；它只接受调用方注入的基准价格，不是 SharedSignals 服务端、数据采集器、current-v1 client 或交易 authority。
- A股 current-v1 的基准/指数/行业环境数据必须先经 `GET /v1/catalog`、`POST /v1/query`、Evidence Gate 和不可变 snapshot，再由 TA 适配为本模块输入；本模块不得自行访问兄弟仓、SQLite、旧专用端点或 provider。
- `shared/review/benchmark.py` 仍是 `RETIREMENT_PENDING_VERIFICATION` 的兼容分析路径，不得作为 current-v1 数据或完成证明；其状态以 `shared/governance/legacy_inventory.yaml` 为准。

## 文件
- `benchmark_tracker.py` — 追踪沪深300/创业板指/买入持有基准, 计算区间收益, 对比组合表现。

## 基准说明
- **CSI300 (沪深300)**: 大盘基准, 代码 000300.SH
- **ChiNext (创业板指)**: 成长股基准, 代码 399006.SZ
- **Buy-Hold (买入持有)**: 等权买入并持有初始组合, 不调仓

## 数据来源
- 基准行情数据通过 `update_benchmark(date)` 注入；current-v1 输入只能来自已通过 TA Evidence Gate 的 catalog/query snapshot，不直接调用 MarketGraph、SharedSignals 服务端内部实现或数据 provider。
- 本地 CSV 存储, 每日一条记录

## 原则
- 基准是衡量 alpha 的标尺, 不可选偏
- 买入持有基准反映"不调仓"的机会成本
- 对比必须同周期、同口径

## 接口
```python
from benchmark_tracker import update_benchmark, get_benchmark_return, compare
```

## 与 review/benchmark.py 的关系
- `benchmark/` (本模块): 数据层 — 采集、存储、计算基准区间收益
- `review/benchmark.py`: 分析层 — alpha/beta/sharpe/最大回撤等统计对比
- 两者互补: 本模块提供原始收益数据, review 模块消费数据进行高级分析
