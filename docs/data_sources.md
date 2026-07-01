# TradingAgent 数据源文档

> **用途**: TradingAgent 交易系统的数据依赖和接入指南  
> **版本**: 1.0.0 | **状态**: active

---

## 目录

1. [概述](#概述)
2. [TradingAgent 使用的 SharedSignals 函数](#tradings-使用的-sharedsignals-函数)
3. [Import 方式](#import-方式)
4. [迁移指南：Ashare → SharedSignals](#迁移指南ashare--sharedsignals)
5. [数据流总览](#数据流总览)

---

## 概述

TradingAgent 是交易执行全闭环系统，负责从筛选到执行到复盘的全流程。按照架构边界：

- **SharedSignals** (→ TradingAgent): 只读行情 + 事件 + 基本面 + 资金数据
- **MarketGraph** (→ TradingAgent): 只读 regime + event_impact + forward_calendar + scenario
- TradingAgent 不回传交易决策给 SharedSignals 或 MarketGraph（保持研究独立）

### 消费者身份

| 模块 | 消费的 SharedSignals 数据 | 用途 |
|------|-------------------------|------|
| `shared/screening/` | 日线行情 + 事件 + 基本面 + 资金流 | 六维打分 |
| `shared/adversarial/` | 行情 + 事件 | 压力测试 + 历史类比 |
| `shared/risk/` | 行情 + 覆盖状态 | 持仓监控 + 巡查 |
| `shared/portfolio/` | 行情 | 风险平价 + 仓位分配 |
| `shared/execution/` | 行情（实时） | 执行 + 滑点建模 |
| `shared/review/` | 行情 + 归因数据 | 日/周复盘 |
| `shared/notify/` | 健康状态 | 告警路由 |
| `shared/accounting/` | 行情 | 持仓记账 + 对账 |

---

## TradingAgent 使用的 SharedSignals 函数

### 核心数据读取 (marketgraph_marketdata_db.py)

所有行情和事件读取通过 `read_daily`、`read_events` 等统一入口：

| 函数 | 用途 | TradingAgent 使用场景 |
|------|------|-----------------|
| `read_daily(market, symbol, ...)` | 日线 OHLCV | 六维打分技术面、持仓估值、回测 |
| `read_events(provider, event_type, ...)` | 新闻/事件流 | 事件面评分，信号检测 |
| `read_intraday(market, symbol, ...)` | 分钟线 | 实时条件监控、盘中触发 |
| `read_crypto_markets()` | Crypto 行情快照 | Crypto shadow/sim 执行 |
| `read_pm_markets(limit)` | 预测市场列表 | PM shadow 执行 |
| `read_pm_prices(limit)` | 预测市场价格 | PM 策略信号 |
| `read_factors(market, factor_name, ...)` | 派生因子 | 因子归因 |
| `coverage_summary()` | 覆盖状态 | 巡查/自愈判断 |
| `health_summary()` | 健康检查 | 告警路由、自愈触发 |

### 交易日历 (market_calendar.py)

| 函数 | 用途 | TradingAgent 使用场景 |
|------|------|-----------------|
| `is_trading_day(date)` | 判断交易日 | 盘前/盘中/盘后任务调度 |
| `get_trading_days(start, end)` | 交易日区间 | 周复盘时间范围计算 |
| `get_next_trading_day(date)` | 下一个交易日 | 资金计划 T+1 日期计算 |

### 统一回测缓存 (backtest_cache)

位于 `/opt/investment/Ashare/data/backtest_cache/`，覆盖 5 个市场：

| 目录 | 覆盖 |
|------|------|
| `daily/` + `daily_basic/` + `index/` | A 股日线、基本面、指数 |
| `hk_etf_daily/` | 港股 ETF 代理（6 ETF + HSI.csv） |
| `us_daily/` | 美股日线 |
| `crypto_daily/` | Crypto 日线（BTC/ETH/BNB/SOL/XRP/ADA/DOGE/AVAX/LINK） |
| `pm_daily/` | Polymarket 预测市场日线 |

### 通过 MarketGraph MCP 消费的数据

| MCP 工具 | TradingAgent 使用场景 |
|---------|-----------------|
| `get_regime` | 宏观面评分、All Weather 倾斜 |
| `get_all_weather_allocation` | 多市场组合分配 |
| `query_event_impact` | 事件→标的映射 |
| `get_decision_draft` | 每日决策草案参考 |
| `news_brief` | 新闻→因果影响排序 |

---

## Import 方式

### 方式 1: 直接 import reader 函数

```python
import sys
from pathlib import Path

# 添加 SharedSignals 到 sys.path
SHARED_SIGNALS = Path("/opt/investment/SharedSignals")
if str(SHARED_SIGNALS) not in sys.path:
    sys.path.insert(0, str(SHARED_SIGNALS))

# 直接 import bridge 中的 reader 函数
from bridge.marketgraph_marketdata_db import (
    read_daily,
    read_events,
    read_intraday,
    read_pm_markets,
    read_pm_prices,
    read_crypto_markets,
    read_factors,
    coverage_summary,
    health_summary,
    read_coverage_status,
)

# 交易日历
from reference.market_calendar import (
    is_trading_day,
    get_trading_days,
    get_next_trading_day,
)
```

### 方式 2: 通过 MCP 工具调用 (远程 agent / 跨进程)

```python
# 使用 MCP client 调用 MarketGraph server
from marketgraph_mcp_client import call_tool

# 读取日线数据
result = call_tool("read_marketdata_db", {
    "dataset": "daily",
    "market": "Ashare",
    "symbol": "600519.SH",
    "limit": 200,
})

# 读取事件
result = call_tool("read_marketdata_db", {
    "dataset": "events",
    "provider": "rss",
    "limit": 100,
})
```

### 方式 3: 使用统一回测缓存

```python
import pandas as pd
from pathlib import Path

BACKTEST_CACHE = Path("/opt/investment/Ashare/data/backtest_cache")

# 读取 A 股日线
ashare_daily = pd.read_csv(BACKTEST_CACHE / "daily" / "600519.csv")

# 读取 Crypto 日线
crypto_daily = pd.read_csv(BACKTEST_CACHE / "crypto_daily" / "BTCUSDT.csv")
```

### 推荐实践

1. **Cron 任务 / 批处理**: 方式 1（直接 import），避免 MCP 开销
2. **远程 agent**: 方式 2（MCP），不持有数据库连接
3. **回测 / 研究**: 方式 3（CSV 缓存），最快、最可复现
4. **所有读取前**: 调用 `health_summary()` 检查数据新鲜度

---

## 迁移指南：Ashare → SharedSignals

### 背景

TradingAgent 的 A 股模块（`Ashare/`）历史上直接调用 Tushare API 和本地 CSV 缓存。按照架构分层，数据读取应统一迁移到 SharedSignals。

### 迁移对照表

| 旧方式 (Ashare 直接调用) | 新方式 (SharedSignals) | 状态 |
|------------------------|----------------------|------|
| `t_plus_1.py` 直接操作 Tushare | `read_daily("Ashare", ...)` | 部分迁移 |
| 本地 CSV 直接读取 | `read_daily("Ashare", ...)` → SQLite | 待迁移 |
| `a_share_tushare_api._call("daily", ...)` | `read_daily("Ashare", ...)` | 待废弃 |
| `market_calendar` 各市场各自实现 | `SharedSignals.reference.market_calendar` 统一 | 已统一 |
| `backtest_cache` 散落各市场 | `/opt/investment/Ashare/data/backtest_cache/` 统一 | 已统一 |
| 实时行情 `stock_quotes` | MCP `stock_quotes` → `read_daily` | 进行中 |

### 迁移步骤

1. **识别**: 找到 TradingAgent 中所有直接调用 Tushare / 本地 CSV 的位置
2. **替换**: 将 `_call("daily", ...)` 替换为 `read_daily("Ashare", ...)`
3. **验证**: 对比新旧输出，确认数据一致性
4. **清理**: 移除旧的 Tushare 直接 import

### 示例迁移

**旧代码 (Ashare/tools/some_scorer.py)**:
```python
from a_share_tushare_api import _call

def get_daily_prices(ts_code, start, end):
    rows = _call("daily", {"ts_code": ts_code, "start_date": start, "end_date": end})
    return [{"date": r["trade_date"], "close": float(r["close"])} for r in rows]
```

**新代码**:
```python
from bridge.marketgraph_marketdata_db import read_daily

def get_daily_prices(ts_code, start, end):
    rows = read_daily("Ashare", symbol=ts_code, start_date=start, end_date=end)
    return [{"date": r["trade_date"], "close": r["close"]} for r in rows]
```

### 注意事项

- SharedSignals 的 `symbol` 参数与 Tushare 的 `ts_code` 格式一致（如 `600519.SH`）
- `read_daily` 按 `trade_date` 升序返回，与 Tushare 降序不同
- 日期格式同时接受 `YYYYMMDD` 和 `YYYY-MM-DD`
- 空结果返回 `[]` 而非抛异常

---

## 数据流总览

```
┌─────────────────────────────────────────────────────┐
│                   SharedSignals                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ Tushare  │  │ Binance  │  │ Polymarket / RSS  │  │
│  │ (14接口) │  │ (4接口)  │  │ (3+883)          │  │
│  └────┬─────┘  └────┬─────┘  └────────┬──────────┘  │
│       │             │                │              │
│       ▼             ▼                ▼              │
│  ┌──────────────────────────────────────────────┐   │
│  │          marketdata.sqlite (11 表)            │   │
│  │  reader 函数: read_daily / read_events / ... │   │
│  └────────────────────┬─────────────────────────┘   │
└───────────────────────┼─────────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────────┐
   │ TradingAgent │  │MarketGraph│  │ 研究工具     │
   │          │  │          │  │              │
   │ • 六维   │  │ • 因果图 │  │ • 回测      │
   │   打分   │  │ • regime │  │ • 因子研究  │
   │ • 执行   │  │ • 联动   │  │ • 参数优化  │
   │ • 复盘   │  │ • 归因   │  │              │
   └──────────┘  └──────────┘  └──────────────┘
```

---

## 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-06-30 | 1.0.0 | 初始版本 |
