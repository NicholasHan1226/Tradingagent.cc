# benchmark/

## 目标
Benchmark tracking for performance comparison.

## 文件
- `benchmark_tracker.py` — 追踪沪深300/创业板指/买入持有基准, 计算区间收益, 对比组合表现。

## 基准说明
- **CSI300 (沪深300)**: 大盘基准, 代码 000300.SH
- **ChiNext (创业板指)**: 成长股基准, 代码 399006.SZ
- **Buy-Hold (买入持有)**: 等权买入并持有初始组合, 不调仓

## 数据来源
- 基准行情数据通过 `update_benchmark(date)` 注入 (生产环境由 daily_runner 从 MarketGraph/Tushare 拉取后注入)
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
