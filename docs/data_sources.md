# Tradings 接入 SharedSignals 数据源指南

> **仓库**: Tradings (`https://github.com/NicholasHan1226/Tradingagent.cc.git`)
> **日期**: 2026-06-30 | **版本**: v1
> **状态**: 8/10 模块已迁移 (Unit 4 完成后)

---

## 1. 概述

Tradings 是交易执行全闭环系统,从 SharedSignals 统一读取数据,不再直接调用 Tushare/Binance/Polymarket 等外部 API。本文档说明 Tradings 各模块如何通过 SharedSignals reader 获取数据。

### 1.1 接入方式

```python
import sys
sys.path.insert(0, '/opt/investment/SharedSignals')
from reader import (
    get_market_data,
    get_events,
    get_capital_flow,
    get_sentiment,
    get_fundamentals,
    is_trading_day,
    get_trading_days,
    get_next_trading_day,
    get_market_indices,
    get_health_status,
)
```

### 1.2 原则

- **单一入口**: 所有行情/事件/基本面/资金/情绪数据只从 SharedSignals reader 读取
- **不直接调外部API**: 严禁直接 import Tushare/Binance/Polymarket 客户端
- **不再读文件**: 不直接读 CSV/SQLite 文件路径
- **降级运行**: 数据不可用时降级,不阻塞流程

---

## 2. 使用的接口

Tradings 使用以下 6 个 SharedSignals reader 函数:

| 函数 | 用途 | 使用模块 |
|------|------|----------|
| `get_market_data` | 日线 OHLCV 行情 | screening, risk, portfolio, execution, accounting, benchmark, review |
| `get_events` | 事件/公告/政策/研报 | screening, adversarial |
| `get_capital_flow` | 主力资金/北向/融资融券 | screening, risk |
| `get_sentiment` | 市场情绪/温度/恐贪 | screening, adversarial |
| `get_fundamentals` | 财务报表/财务指标 | screening |
| `is_trading_day` | 交易日判断 | t_plus_1, execution, review, screening |

辅助接口(按需):

| 函数 | 用途 |
|------|------|
| `get_trading_days` | 日期范围内交易日列表 |
| `get_next_trading_day` | 下一个交易日 |
| `get_market_indices` | 大盘指数快照 |
| `get_health_status` | 数据健康检查(patrol/heal 前检查) |

---

## 3. 各模块数据需求

### 3.1 screening (筛选层)

六维打分系统需要全维度数据:

| 维度 | 权重 | 数据源 | 调用 | 说明 |
|------|------|--------|------|------|
| 宏观面 | 0.15 | MarketGraph regime (通过 get_macro_factors 间接) | `get_market_indices("Ashare")` | 配合 MarketGraph regime 判断经济季节 |
| 事件面 | 0.20 | SharedSignals events | `get_events(market="Ashare", ...)` | 公告/政策/研报影响方向与强度 |
| 基本面 | 0.25 | SharedSignals fundamentals | `get_fundamentals(ts_code, "indicators", ...)` | ROE/PE/营收增速/毛利率 |
| 资金面 | 0.15 | SharedSignals capital_flow | `get_capital_flow("Ashare", "moneyflow", trade_date)` | 主力净流入/北向持仓 |
| 技术面 | 0.15 | SharedSignals market_data | `get_market_data("Ashare", ts_code, ...)` | 动量/弹性/突破/均线 |
| 情绪面 | 0.10 | SharedSignals sentiment | `get_sentiment("Ashare", "all", ...)` | 换手率/涨跌比/温度 |

```python
# 示例: screening 六维打分入口
from reader import get_market_data, get_events, get_capital_flow, get_sentiment, get_fundamentals

def compute_six_dimension_score(ts_code, trade_date):
    # 基本面
    fundamentals = get_fundamentals(ts_code, "indicators", "20250101", trade_date)
    
    # 技术面 (行情)
    market = get_market_data("Ashare", ts_code, 
                            start_date=get_trading_days(trade_date, trade_date, -30), 
                            end_date=trade_date)
    
    # 资金面
    flow = get_capital_flow("Ashare", "moneyflow", trade_date)
    
    # 事件面
    events = get_events(market="Ashare", start_date=trade_date, end_date=trade_date, limit=100)
    
    # 情绪面
    sentiment = get_sentiment("Ashare", "all", trade_date, trade_date)
    
    # ... 六维合成逻辑
```

### 3.2 risk (风控层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| pre_trade_check | 当前持仓行情 | `get_market_data("Ashare", pos_codes, trade_date, trade_date)` |
| position_monitor | 持仓实时监控 | `get_market_data("Ashare", pos_codes, ...)` |
| black_swan | 极端行情/流动性 | `get_market_data("Ashare", all_codes, ...)` + `get_capital_flow` |
| patrol | 数据健康检查 | `get_health_status()` |

```python
# 示例: pre_trade_check
from reader import get_market_data

def check_position_risk(positions, trade_date):
    codes = ",".join([p.ts_code for p in positions])
    latest = get_market_data("Ashare", codes, trade_date, trade_date)
    # ... 计算最大回撤/波动率/集中度
```

### 3.3 review (复盘层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| daily_review | 全天行情+交易记录 | `get_market_data("Ashare", all_positions, trade_date, trade_date)` |
| attribution | 收益归因 | `get_market_data` + `get_events` + `get_capital_flow` |
| weekly/monthly_review | 周/月趋势 | `get_market_data` (长周期) |
| benchmark_compare | 基准对比 | `get_market_indices("Ashare")` |
| self_heal_loop | 复盘→调权闭环 | 所有维度数据 |

```python
# 示例: daily_review
from reader import get_market_data, get_market_indices

def daily_pnl_check(positions, trade_date):
    codes = ",".join([p.ts_code for p in positions])
    prices = get_market_data("Ashare", codes, trade_date, trade_date)
    indices = get_market_indices("Ashare")
    # ... 计算当日盈亏 + vs 基准
```

### 3.4 portfolio (组合层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| constructor | 候选池估值 | `get_market_data("Ashare", candidates, ...)` |
| position_sizer | 波动率计算 | `get_market_data("Ashare", codes, start-60, end)` |
| rebalancer | 调仓判断 | `get_market_data` |
| exit_manager | 止损/止盈 | `get_market_data("Ashare", pos_codes, trade_date, trade_date)` |

### 3.5 adversarial (对抗分析层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| bull_bear_debate | 事件/情绪/基本面 | `get_events` + `get_sentiment` + `get_fundamentals` |
| historical_analogy | 历史行情类比 | `get_market_data` (长周期) |
| stress_test | 极端行情回测 | `get_market_data` (历史极端区间) |

### 3.6 execution (执行层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| signal_state_machine | 条件触发判断 | `get_market_data` (盘中分钟级) + `is_trading_day` |
| slippage_model | 滑点计算 | `get_market_data` |
| sim_broker | 模拟盘 | `get_market_data` |
| shadow_broker | 影子盘 | `get_market_data` |

### 3.7 accounting (记账层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| capital_ledger | 净值计算 | `get_market_data` (收盘价) |
| daily_reconcile | 对账 | `get_market_data` (收盘价 vs 成交价) |

### 3.8 benchmark (基准层)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| benchmark_tracker | 沪深300/创业板 | `get_market_indices("Ashare")` + `get_market_data("Ashare", "000300.SH", ...)` |

### 3.9 t_plus_1 (A股T+1)

| 功能 | 数据需求 | 调用 |
|------|----------|------|
| can_sell | 判断是否可卖 | `is_trading_day` + `get_trading_days` |

### 3.10 notify (通知层)

不需要直接调用 SharedSignals,但从 screening/risk/review 获得的数据已包含 SharedSignals 来源信息。

---

## 4. 迁移指南: Ashare 旧路径 → SharedSignals reader

以下对照表列出 Ashare 旧工具中直接调 Tushare/读文件的代码,如何迁移到 SharedSignals reader。

### 4.1 行情数据

| 旧方式 (Ashare 工具) | 新方式 (SharedSignals reader) |
|----------------------|-------------------------------|
| `a_share_tushare_api.get_daily(ts_code, start, end)` | `get_market_data("Ashare", ts_code, start, end)` |
| `a_share_tushare_api.get_indices()` | `get_market_indices("Ashare")` |
| `a_share_tushare_api.get_stock_minutes(ts_code, date)` | `get_market_data("Ashare", ts_code, date, date)` (分钟级后续支持) |
| `pd.read_csv(f"{DATA_DIR}/daily/{ts_code}.csv")` | 禁止 — 用 `get_market_data` |
| `pd.read_csv("Ashare/data/backtest_cache/daily/...")` | 禁止 — 用 `get_market_data` |

### 4.2 资金数据

| 旧方式 | 新方式 |
|--------|--------|
| `a_share_tushare_api.get_moneyflow(trade_date)` | `get_capital_flow("Ashare", "moneyflow", trade_date)` |
| `a_share_tushare_api.get_margin(trade_date)` | `get_capital_flow("Ashare", "margin", trade_date)` |
| `a_share_tushare_api.get_hk_hold(trade_date)` | `get_capital_flow("Ashare", "northbound", trade_date)` |
| `pd.read_csv("Ashare/data/moneyflow/...")` | 禁止 — 用 `get_capital_flow` |

### 4.3 事件数据

| 旧方式 | 新方式 |
|--------|--------|
| `pd.read_csv("MarketGraph/data/intake/event_candidates.csv")` | 禁止 — 用 `get_events(market="Ashare", ...)` |
| `MarketGraphRuntime/staging/event_candidates/*.ndjson` | 禁止 — 用 `get_events` |
| Tushare `news` API 直接调用 | 禁止 — 用 `get_events` |

### 4.4 基本面数据

| 旧方式 | 新方式 |
|--------|--------|
| `a_share_tushare_api.get_income(ts_code, start, end)` | `get_fundamentals(ts_code, "income", start, end)` |
| `a_share_tushare_api.get_balancesheet(...)` | `get_fundamentals(ts_code, "balance", ...)` |
| `a_share_tushare_api.get_fina_indicator(...)` | `get_fundamentals(ts_code, "indicators", ...)` |

### 4.5 交易日历

| 旧方式 | 新方式 |
|--------|--------|
| `Ashare.t_plus_1.can_sell(buy_date, today)` (自然日计算) | `is_trading_day(date)` + `get_trading_days(start, end)` (交易日计算) |
| `MarketGraph/reference/market_calendar.py` | `is_trading_day` / `get_next_trading_day` / `get_trading_days` |

### 4.6 情绪数据

| 旧方式 | 新方式 |
|--------|--------|
| `pd.read_csv("MarketGraph/data/sentiment_signals.csv")` | 禁止 — 用 `get_sentiment("Ashare", ...)` |
| 手工计算涨跌比/换手率 | `get_sentiment("Ashare", "advance_decline", ...)` |

---

## 5. 当前状态

### 5.1 模块迁移状态

| # | 模块 | 路径 | 状态 | 迁移内容 |
|---|------|------|------|----------|
| 1 | screening | `shared/screening/` | ✅ 已迁移 | 六维打分 reader 接入 + 旧路径清理 |
| 2 | risk | `shared/risk/` | ✅ 已迁移 | pre_trade_check + position_monitor reader 接入 |
| 3 | review | `shared/review/` | ✅ 已迁移 | daily/weekly/monthly review reader 接入 |
| 4 | portfolio | `shared/portfolio/` | ✅ 已迁移 | constructor/sizer/rebalancer reader 接入 |
| 5 | adversarial | `shared/adversarial/` | ✅ 已迁移 | bull_bear/stress_test reader 接入 |
| 6 | execution | `shared/execution/` | ✅ 已迁移 | state_machine/sim/shadow broker reader 接入 |
| 7 | accounting | `shared/accounting/` | ✅ 已迁移 | ledger/reconcile reader 接入 |
| 8 | benchmark | `shared/benchmark/` | ✅ 已迁移 | tracker reader 接入 |
| 9 | t_plus_1 | `Ashare/t_plus_1.py` | ⬜ 待迁移 | `can_sell` 改用 `is_trading_day` + `get_trading_days` |
| 10 | notify | `shared/notify/` | ⬜ 待迁移 | 不直接读数据,但需确保来源标注为 SharedSignals |

### 5.2 市场模块迁移状态

| 市场 | 路径 | 状态 | 说明 |
|------|------|------|------|
| Ashare | `Ashare/` | 🟡 部分迁移 | 144工具,旧 Tushare 路径逐步替换 |
| Crypto | `Crypto/` | ✅ 已迁移 | 21工具 symlink,已迁移到 reader |
| US | `US/` | ✅ 已迁移 | 20工具 symlink,已迁移到 reader |
| PM | `PM/` | ✅ 已迁移 | 20工具 symlink,已迁移到 reader |
| HK | `HK/` | ⬜ 预留 | 空壳,待激活后迁移 |

### 5.3 数据流示意

```
                    ┌─────────────────────────────────┐
                    │       SharedSignals              │
                    │  ┌───────────────────────────┐  │
                    │  │       reader.py            │  │
                    │  │  get_market_data           │  │
                    │  │  get_events                │  │
                    │  │  get_capital_flow          │  │
                    │  │  get_sentiment             │  │
                    │  │  get_fundamentals          │  │
                    │  │  is_trading_day            │  │
                    │  └───────────────────────────┘  │
                    └─────────────┬───────────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
          ▼                       ▼                       ▼
   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
   │  screening   │      │    risk      │      │   review     │
   │  6维打分     │      │  风控检查    │      │  日/周/月复盘│
   └──────────────┘      └──────────────┘      └──────────────┘
          │                       │                       │
          └───────────────────────┼───────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │   Tradings 交易闭环      │
                    │   portfolio → execution  │
                    │   → accounting → notify  │
                    └─────────────────────────┘
```

### 5.4 已知缺口

1. **t_plus_1 迁移**: `can_sell` 仍用自然日计算,需切换到 `is_trading_day` + `get_trading_days`
2. **分钟级行情**: `get_market_data` 当前只支持日线,分钟线待 SharedSignals 补充
3. **Crypto 链上数据**: 当前 reader 只支持 K线,链上/TVL/合约数据待扩展
4. **PM 结算数据**: 预测市场结算状态待补充

---

## 6. 附录

### 6.1 相关文档

- SharedSignals: `/opt/investment/SharedSignals/API_CONTRACT.md`
- SharedSignals: `/opt/investment/SharedSignals/README.md`
- MarketGraph: `/opt/investment/MarketGraph/docs/data_sources.md`
- Handoff: `/opt/investment/Tradings/docs/HANDOFF_架构对齐_20260630.md`

### 6.2 快速检查清单

Consumer agent 接入 Tradings 新模块时:

- [ ] 不直接 import Tushare/Binance/Polymarket
- [ ] 不直接读 CSV/SQLite 文件路径
- [ ] 所有数据通过 `from reader import ...` 获取
- [ ] 数据不可用时降级运行,不阻塞
- [ ] 记录数据来源(`source="SharedSignals"`)用于复盘
- [ ] 锁定 API 版本 v1
