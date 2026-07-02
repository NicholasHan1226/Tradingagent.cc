# TradingAgent 影子盘市场配置设计

> **日期**: 2026-06-30
> **作者**: Claude (只读分析, 不修改文件)
> **目标**: 为 4 市场 (A股/加密/美股/预测市场) 设计影子盘配置, 供 W4 阶段实现各市场 MarketAdapter。
> **依赖**: SharedSignals (数据源), shared/ 通用层, W2-1 调度+MarketAdapter 接口设计

---

## 0. 共通: MarketAdapter 接口契约

所有市场 adapter 必须实现以下接口 (接口定义将在 W2-1 定型, 以下为设计契约):

```python
class MarketAdapter(Protocol):
    """每个市场实现一个 Adapter, 插入 shared/ 管道."""

    # ── 市场标识 ──
    market: str                          # "ashare" | "crypto" | "us" | "pm"

    # ── universe ──
    def get_universe(self, date: str) -> list[str]:
        """返回该市场当日可交易标的列表 (symbol 格式)."""

    def universe_filter(self, symbols: list[str], date: str) -> list[str]:
        """过滤不可交易标的 (停牌/ST/新股/流动性不足等)."""

    # ── symbol 映射 ──
    def map_symbol(self, ts_code: str) -> str:
        """将外部 ts_code 映射为 reader 可查询的 symbol.
           e.g. "600519.SH" → "600519" (Ashare)"""

    def external_symbol(self, symbol: str) -> str:
        """反向映射: 内部 symbol → 外部 ts_code. 用于 shadow_broker 记录."""

    # ── 数据 ──
    def get_bars(self, symbol: str, start: str, end: str) -> list[dict]:
        """日线 OHLCV. 来自 SharedSignals market_bars_daily."""

    def get_factors(self, symbol: str) -> list[dict]:
        """因子/资金流. 来自 SharedSignals market_factors."""

    def get_events(self, symbol: str, start: str, end: str) -> list[dict]:
        """事件. 来自 SharedSignals market_events."""

    # ── 策略 ──
    def get_strategy_config(self) -> dict:
        """返回影子盘策略清单: {strategy_name: {type, params, source_tool}}."""

    # ── 执行 ──
    def get_shadow_account(self) -> dict:
        """影子盘账户初始配置: {initial_capital, currency, fee_rate, constraints}."""

    def get_trading_calendar(self) -> TradingCalendar:
        """交易日历: is_trading_day, next_trading_day, session_times."""

    # ── 风控 ──
    def get_risk_limits(self) -> dict:
        """市场特定风控参数覆盖."""
```

### 0.1 共通约束

| 项目 | 规则 |
|------|------|
| **capital_layer** | 全程 `"shadow"`, 不得写 `"real"` 或 `"simulated"` |
| **数据缺失** | fail-safe 降级 0.5 (中性), 标记 `stale=True` |
| **reader 对接** | 统一通过 `SharedSignalsReader(market="xxx")` 查询; `TradingAgentDataReader` 做 facade |
| **shadow_broker** | 共享 `shared/execution/shadow_broker.py`, 只记录不执行; `ShadowTrade` schema: `trade_id / strategy_name / trade_date / ts_code / side / quantity / price / amount / commission / net_amount / capital_layer / status / created_at / note` |
| **信号状态机** | 共享 `pending → claimed → running → filled \| expired \| cancelled \| failed \| partial` |
| **信号文件路径** | `signals/pending/` `signals/filled/` `signals/positions/` `signals/cancelled/` |
| **复盘频率** | 每日 2 次 (午盘 + 收盘, 或等效时段) |
| **策略晋级** | shared promotion 机制: `research → shadow → sim → real`, shadow 需 ≥100 trades / ≥60% positive days / ≤10% max drawdown 方可晋级 sim |

---

## 1. A股影子盘 (Ashare)

### 1.1 市场标识

- `market = "ashare"`
- 交易时段: 09:15–11:30, 13:00–15:00 (北京时间)
- 特殊阶段: 集合竞价 (09:15–09:25, 09:20 后不可撤单), 收盘竞价 (14:57–15:00)
- T+1: 当日买入次日方可卖出 (用交易日历, 非自然日)

### 1.2 Universe 来源

**主表**: `SharedSignals.market_assets` WHERE `market = "Ashare"`
- 字段: `market, symbol, name, asset_type, exchange, sector, list_date, status`
- symbol 格式: 纯数字代码, 如 `"600519"`, `"000001"`
- 约 5000+ 标的 (含京沪深)

**辅助**: `SharedSignals.market_coverage_status` WHERE `market = "Ashare"`

**universe_filter 规则** (对应 `Ashare/market_phases/` 和 `shared/screening/condition_generator.py` 现有逻辑):

| 过滤条件 | 实现方式 | 备注 |
|----------|----------|------|
| ST / *ST | `name` 含 "ST" 或 `status` 字段 | 现有 `universe_filter` 已实现 |
| 停牌 | `market_coverage_status.coverage_status != "normal"` | 现有已实现 |
| 新股 (<60 交易日) | `list_date` < 60 天前 | 避免次新股波动 |
| 涨跌停 (±10% / ±20%) | 当日 `bars_daily.close` ≈ `pre_close * 1.10` 或 `* 0.90` | 科创/创业板 ±20% |
| 流动性不足 | `bars_daily.amount` < 阈值 (如 5000 万) 或 `market_factors.turnover_rate` < 0.5% | 避免无量标的 |
| 北交所 (可选) | `exchange = "BSE"` 30% 涨跌停, 流动性更低 | 暂过滤 |

**注意**: `shared/screening/six_dimension_scorer.py` 当前硬编码 `market="Ashare"` 调 `get_factors("Ashare", ...)` / `get_bars_daily("Ashare", ...)`. W2-1 需改为注入 `market` 参数。

### 1.3 Symbol 映射

| 方向 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 外部→内部 | `"600519.SH"` (ts_code) | `"600519"` (symbol) | 去掉 `.SH`/`.SZ`/`.BJ` 后缀 |
| 内部→外部 | `"600519"` (symbol) | `"600519.SH"` (ts_code) | 查 `market_assets.exchange` 补后缀 |
| reader 查询 | `reader.get_bars_daily("Ashare", "600519", ...)` | — | `market="Ashare"`, `symbol` 为纯数字 |

已有工具: `shared/screening/six_dimension_scorer.py` 的 `_strip_suffix()` 和 `_symbol_variants()` 处理映射。

### 1.4 数据来源

所有数据从 SharedSignals 单一入口读取:

| 数据 | SharedSignals 表 | 查询参数 | 频率 | 备注 |
|------|-----------------|----------|------|------|
| 日线 OHLCV | `market_bars_daily` | `market="Ashare", symbol` | 日频 | 含 `open/high/low/close/volume/amount` |
| 分钟线 | `market_bars_intraday` | `market="Ashare", symbol, interval="5m"` | 5 分钟 | 集合竞价/盘中监控 |
| 事件 | `market_events` | `market="Ashare", symbol` | 实时 | 公告/新闻/财报 |
| 因子/资金流 | `market_factors` | `market="Ashare", symbol` | 日频 | PE/PB/turnover/net_mf_amount/... |
| 资产元数据 | `market_assets` | `market="Ashare", symbol` | 静态 | 行业/上市日期/交易所 |
| 覆盖状态 | `market_coverage_status` | `market="Ashare", date` | 日频 | 停牌/正常 |

**资金流因子** (A股特有, 在 `market_factors`):
- `net_mf_amount` — 主力净流入
- `net_mf_amount_super_lg` / `lg` / `md` / `sm` — 超大单/大单/中单/小单
- `turnover_rate` — 换手率
- `volume_ratio` — 量比
- `pe_ttm` / `pb` — 估值

**MarketGraph 研究结论** (CSV, 经文件读):
- `all_weather_regime.csv` — 宏观 regime (桥水 4 象限)
- `event_candidates.csv` — 事件候选标的
- `sentiment_signals.csv` — 舆情信号

### 1.5 建议策略清单 (从旧 Ashare 工具迁移)

旧系统 144 个工具中, 影子盘相关策略/条件:

| 策略名 | 类型 | 旧工具 | 说明 |
|--------|------|--------|------|
| `breakout_momentum` | 突破/动量 | `a_share_momentum_shadow_strategy.py` | 近期限动量影子验证, 量价突破 + 均线多头 |
| `pullback_scan` | 回调/龙头回踩 | `a_share_pullback_scan.py` | 强势股回调至均线支撑, 缩量企稳后介入 |
| `etf_rotation` | ETF 轮动 | `a_share_return_enhancement_plan.py` (style rotation) | 行业/风格 ETF 轮动, 基于 regime 配置 |
| `high_elasticity` | 高弹性 | `a_share_high_elasticity_scout.py` / `a_share_early_elasticity_radar.py` | 高波动标的影子盘捕捉 |
| `candidate_discovery` | 候选发现 | `a_share_candidate_discovery_enhancer.py` | 多因子综合排序候选池 |
| `money_flow` | 资金流 | `a_share_money_confirmation_ladder.py` | 主力资金确认梯次入场 |
| `opening_auction` | 集合竞价 | `Ashare/market_phases/opening_auction.py` (STUB) | 竞价量价异常检测 (需实现) |
| `closing_auction` | 收盘竞价/逆回购 | `Ashare/market_phases/closing_auction.py` (STUB) | 闲置资金逆回购 (需实现) |

**策略权重配置** (六维打分 → 条件生成 → shadow 执行):
- 现有 `shared/screening/weights.yaml` 可直接复用 (macro 0.15 / event 0.20 / fundamental 0.25 / capital 0.15 / technical 0.15 / sentiment 0.10)
- 各策略可 override 权重 (如 momentum 策略提高 technical 权重)

**condition 模板** (现有 `condition_generator.py` 支持):
- `breakout` — 突破前高/均线
- `pullback` — 回调至支撑
- `event` — 事件驱动
- `value` — 低估值
- `rotation` — 轮动信号

### 1.6 Shadow 执行注意点

| 注意点 | 详情 |
|--------|------|
| **T+1 强制** | 买入当日不可卖出; 使用交易日历 `next_trading_day()` 计算 `sellable_date`; 现有 `Ashare/t_plus_1.py` 已实现 `can_sell()` (需修复自然日→交易日) |
| **涨跌停** | 涨停板无法买入 (除非开板); 跌停板无法卖出; universe_filter 标记 |
| **集合竞价** | 09:15–09:25 竞价阶段, 09:20 前可撤单; `market_phases/opening_auction.py` (STUB) 需实现 gap/surge/VWAP 检测 |
| **逆回购** | 收盘闲置现金自动 GC-001 (204001); 现有 `capital_plan.py` 已实现 `suggest_reverse_repo()` |
| **shadow_broker** | 使用共享 `shadow_broker.record_shadow()`, `capital_layer="shadow"`; 写入 `shared/data/shadow/` 目录下 |
| **信号文件** | `signals/pending/` 生成信号卡 → condition_monitor 检测触发 → `signals/filled/` |
| **模拟盘 vs 影子盘** | A股模拟盘必须经 TradingAgent `Ashare/sim_executor.py` → Mini receiver/executor → `signals/filled|positions` 回写；影子盘纯服务器端记录 |
| **资金规划** | 初始 shadow 资金 200,000 (10x 实盘, 便于精度); 2–3 只持仓, 每只 5–7 万 |

### 1.7 Reader 对接方式

```python
# Adapter 初始化
from shared.data.reader import TradingAgentDataReader

class AshareAdapter:
    market = "ashare"

    def __init__(self):
        self.reader = TradingAgentDataReader()  # 内部自动 market="Ashare"

    def get_bars(self, symbol: str, start: str, end: str):
        return self.reader.reader.get_bars_daily("Ashare", symbol, start, end)

    def get_factors(self, symbol: str):
        return self.reader.reader.get_factors("Ashare", symbol)

    def get_events(self, symbol: str, start: str, end: str):
        return self.reader.reader.get_events("Ashare", symbol, start, end)

    def get_universe(self, date: str):
        coverage = self.reader.reader.get_coverage("Ashare", date)
        return [r["symbol"] for r in coverage if r.get("coverage_status") == "normal"]

    def universe_filter(self, symbols, date):
        # 1. 过滤 ST/停牌
        # 2. 过滤涨跌停
        # 3. 过滤新股 (<60 天)
        # 4. 过滤流动性不足
        ...
```

**注意**: `TradingAgentDataReader` 当前交易日历方法 (`is_trading_day`/`next_trading_day`) 仅支持 A股, 需在 W1-1/W2-2 阶段扩展为多市场。

---

## 2. 加密影子盘 (Crypto)

### 2.1 市场标识

- `market = "crypto"`
- 交易时段: 7×24, 无休市
- 无 T+1, 无涨跌停, 无集合竞价
- 数据源: Binance (4 接口: spot klines / ticker / depth / exchangeInfo)

### 2.2 Universe 来源

**主表**: `SharedSignals.market_assets` WHERE `market = "Crypto"`
- symbol 格式: 交易对, 如 `"BTCUSDT"`, `"ETHUSDT"`, `"BNBUSDT"`
- 预计 universe 规模: 10–50 个主流交易对 (USDT 本位)

**universe_filter 规则**:

| 过滤条件 | 实现方式 |
|----------|----------|
| 交易量不足 | `market_bars_daily.volume` (USDT 计) < 阈值 |
| 新上市代币 | `list_date` < 30 天 |
| 已下架 | `status != "TRADING"` |
| 非 USDT 交易对 | `symbol` 不含 "USDT" (暂只做 USDT 本位) |
| 低流动性 | bid-ask spread > 0.5% (需从 `market_factors` 获取) |

### 2.3 Symbol 映射

| 方向 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 外部→内部 | `"BTCUSDT"` | `"BTCUSDT"` | Crypto 的 ts_code 即 symbol, 无需转换 |
| reader 查询 | `reader.get_bars_daily("Crypto", "BTCUSDT", ...)` | — | `market="Crypto"` |

### 2.4 数据来源

| 数据 | SharedSignals 表 | 查询参数 | 频率 | 备注 |
|------|-----------------|----------|------|------|
| 日线 OHLCV | `market_bars_daily` | `market="Crypto", symbol` | 日频 | Binance 日线 |
| 分钟线 | `market_bars_intraday` | `market="Crypto", symbol, interval` | 1m/5m/15m/1h | Binance klines |
| 事件 | `market_events` | `market="Crypto", symbol` | 实时 | 链上数据/新闻 |
| 因子 | `market_factors` | `market="Crypto", symbol` | 日频 | volatility / volume_profile / dominance |
| 资产元数据 | `market_assets` | `market="Crypto", symbol` | 静态 | base_asset / quote_asset |

**Crypto 特有因子** (在旧系统 `crypto_market_data.py` 中, 待迁入 `market_factors`):
- `volatility_30d` — 30 日波动率
- `volume_ratio` — 量比
- `rsi_14` — RSI
- `btc_correlation` — BTC 相关性
- `market_cap` / `dominance` — 市值占比

### 2.5 建议策略清单 (从旧 Crypto 工具迁移)

旧系统 21 个工具, 影子盘核心:

| 策略名 | 类型 | 旧工具 | 说明 |
|--------|------|--------|------|
| `momentum_breakout` | 动量突破 | `crypto_shadow_runner.py` (multi-strategy) | 突破 N 日高点 + 放量确认 |
| `trend_following` | 趋势跟踪 | `crypto_portfolio_optimizer.py` | EMA 多周期排列, 顺势持仓 |
| `mean_reversion` | 均值回归 | 隐含在 shadow styles | RSI 超买超卖 + 布林带回归 |
| `volatility_adaptive` | 波动率自适应 | `crypto_portfolio_optimizer.py` | 波动率调整仓位, 高波动减仓 |
| `intraday_signal` | 多周期信号 | `crypto_intraday_signal.py` | 多时间框架信号增强 |

**shadow styles** (旧系统 `crypto_shadow_styles.py` 已定义):
- `frozen_baseline` — 等权持有 BTC/ETH 基准
- `edge_x_kelly` — 信号边沿 × Kelly 仓位
- `vol_target` — 目标波动率 15% 自适应

**注意**: Crypto 的六维打分维度需调整 — `fundamental` 维度不适用 (代币无财报), 权重转移至 `technical` + `sentiment`。

### 2.6 Shadow 执行注意点

| 注意点 | 详情 |
|--------|------|
| **7×24** | 无休市, 条件监控需持续运行; 复盘频率改为 00:00 UTC / 12:00 UTC 各一次 |
| **无 T+1** | 当日可自由买卖; `sellable_date` = 买入日期 |
| **波动率** | Crypto 年化波动 60–100%, A 股仅 20%; `risk_limits.yaml` 需独立配置 (stop_loss 放宽至 -15%, max_positions 可多至 8–10) |
| **手续费** | Binance spot: maker 0.1% / taker 0.1% (可 BNB 折扣); `shadow_broker` 需记录 commission |
| **滑点** | 影子盘假设 taker 成交, 使用 `market_bars_daily.close` 作为成交价; 高波动时段需加滑点估算 |
| **基准** | BTC/USDT 或等权 BTC+ETH 作为 benchmark |
| **资金** | 初始 shadow 资金 100,000 USDT; 仓位 5–10 个交易对 |

### 2.7 Reader 对接方式

```python
class CryptoAdapter:
    market = "crypto"

    def __init__(self):
        self.reader = TradingAgentDataReader()

    def get_bars(self, symbol: str, start: str, end: str):
        return self.reader.reader.get_bars_daily("Crypto", symbol, start, end)

    # ... 同上模式, market="Crypto"
```

---

## 3. 美股影子盘 (US)

### 3.1 市场标识

- `market = "us"`
- 交易时段: 21:30–04:00 (北京时间, 夏令时) / 22:30–05:00 (冬令时)
- 盘前: 16:00–21:30, 盘后: 04:00–08:00
- T+2 结算 (但对影子盘无影响, 影子盘纯记录)
- PDT (Pattern Day Trader) 规则: 5 日内 3 次日内交易 → 实盘需 $25k 最低保证金; 影子盘不适用

### 3.2 Universe 来源

**主表**: `SharedSignals.market_assets` WHERE `market = "US"`
- symbol 格式: ticker, 如 `"AAPL"`, `"TSLA"`, `"MSFT"`
- 预计 universe 规模: 500–2000 只 (S&P 500 + Nasdaq 100 + 中概股)

**辅助**: Tushare `us_daily` / Alpaca API 补充

**universe_filter 规则**:

| 过滤条件 | 实现方式 |
|----------|----------|
| 价格过低 (penny stock) | `close < $5` |
| 交易量不足 | `volume < 100000` (日均) |
| 市值过小 | `market_factors.market_cap` < $1B |
| 新上市 | `list_date` < 90 天 |
| 非美元计价 | 仅 US 交易所 |
| ETF/ETN | `asset_type = "ETF"` 可选过滤或独立策略 |

### 3.3 Symbol 映射

| 方向 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 外部→内部 | `"AAPL"` | `"AAPL"` | 直接 ticker, 无需转换 |
| reader 查询 | `reader.get_bars_daily("US", "AAPL", ...)` | — | `market="US"` |

### 3.4 数据来源

| 数据 | SharedSignals 表 | 查询参数 | 频率 | 备注 |
|------|-----------------|----------|------|------|
| 日线 OHLCV | `market_bars_daily` | `market="US", symbol` | 日频 | Alpaca / Tushare |
| 分钟线 | `market_bars_intraday` | `market="US", symbol, interval` | 1m/5m | Alpaca 实时 |
| 事件 | `market_events` | `market="US", symbol` | 实时 | 财报/新闻 |
| 因子 | `market_factors` | `market="US", symbol` | 日频 | PE/市值/股息率 |
| 资产元数据 | `market_assets` | `market="US", symbol` | 静态 | sector/industry |

**US 特有因子** (旧系统 `us_alpaca_market_data.py` + `us_market_data.py`):
- `market_cap` — 市值
- `pe_ratio` / `forward_pe` — 估值
- `dividend_yield` — 股息率
- `short_float` — 空头占比
- `beta` — 相对于 SPY 的 beta

### 3.5 建议策略清单 (从旧 US 工具迁移)

旧系统 20 个工具:

| 策略名 | 类型 | 旧工具 | 说明 |
|--------|------|--------|------|
| `momentum` | 动量 | `us_shadow_runner.py` | 中期动量 (6–12 个月), 买强卖弱 |
| `value` | 价值 | `us_portfolio_optimizer.py` | 低 PE/PB + 高股息 + 质量因子 |
| `earnings_drift` | 财报后漂移 | `us_forward_validation.py` 隐含 | 财报超预期后趋势跟踪 |
| `sector_rotation` | 板块轮动 | `us_opportunity_funnel.py` | regime-based 板块配置 |
| `trend_following` | 趋势跟踪 | `us_daily_runner.py` | MA 交叉 + 波动率过滤 |

**注意**: 美股的六维打分 — `fundamental` 和 `sentiment` 维度权重可提高 (财报数据丰富、社交媒体情绪可获取)。

### 3.6 Shadow 执行注意点

| 注意点 | 详情 |
|--------|------|
| **时区** | 北京时间夜间交易 (21:30–04:00); 审查时段: 北京时间 11:00 (盘中回顾) + 06:00 (收盘复盘) |
| **盘前盘后** | 流动性差, spread 大; 影子盘可忽略盘前盘后或标记 `session="after_hours"` |
| **T+2** | 结算 T+2, 影子盘无影响; 但实盘需考虑资金到账 |
| **拆股/分红** | 需复权处理 (`market_bars_daily` 的 open/high/low/close 应为复权价); `adjusted_close` 字段如有则使用 |
| **PDT** | 影子盘不适用; 模拟盘需标记日内交易次数 (≥4 次/5 日触发审查) |
| **基准** | SPY (S&P 500 ETF) 或 QQQ (Nasdaq 100) |
| **资金** | 初始 shadow 资金 $200,000; 仓位 5–10 只, 单只 ≤$30k |
| **Alpaca 路径** | 美股是唯一具备真实 API 执行能力的市场 (`us_alpaca_executor.py`); 影子盘可复用 Alpaca Paper Trading 获取成交价 (而非 close 估算) |

### 3.7 Reader 对接方式

```python
class USAdapter:
    market = "us"

    def __init__(self):
        self.reader = TradingAgentDataReader()

    def get_bars(self, symbol: str, start: str, end: str):
        return self.reader.reader.get_bars_daily("US", symbol, start, end)

    # ... 同上模式, market="US"
```

---

## 4. 预测市场影子盘 (PM)

### 4.1 市场标识

- `market = "pm"`
- 交易时段: 7×24, Polymarket CLOB
- 独特之处: **概率交易** (YES/NO 合约), 非方向性
- 盈亏单位: 合约价格 ∈ [0, 1] (USDC 计价)
- 到期结算: 事件结果确定后, 赢方合约→ $1, 输方→ $0

### 4.2 Universe 来源

**主表**: `SharedSignals.market_pm_markets`
- 字段 (参考 `docs/data_contract.md` 记录, 实际 schema 在 SharedSignals 仓库): `market_id, title, description, category, volume, liquidity, end_date, status, resolution_source`
- market_id 格式: Polymarket slug 或 token ID, 如 `"will-btc-hit-100k-in-2025"` 

**辅助**: `SharedSignals.market_pm_prices` — 价格时序

**universe_filter 规则**:

| 过滤条件 | 实现方式 |
|----------|----------|
| 流动性不足 | `market_pm_prices` 的 spread > 2% 或 bid/ask 深度不足 |
| 已结算 | `status = "RESOLVED"` |
| 到期过远 | `end_date` > 90 天后 (避免资金锁死) |
| 交易量过低 | `volume` < $5000 (24h) |
| 争议市场 | `category` 含 "social" / "entertainment" (低信号纯度, 可选过滤) |
| 信息不对称 | 无公开可验证结果 (如 "will person X tweet Y") |

### 4.3 Symbol 映射

| 方向 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 外部→内部 | `"will-btc-hit-100k-in-2025"` | `"will-btc-hit-100k-in-2025"` | market_id 即 symbol |
| reader 查询 | PM 专用 reader 方法 (待实现) | — | `market_pm_markets` / `market_pm_prices` |

**注意**: PM 的 `market_pm_markets` 和 `market_pm_prices` 在 `SharedSignalsReader` 中尚未封装查询方法。需要在 reader.py 中新增:
```python
def get_pm_market(self, market_id: str) -> dict | None
def get_pm_prices(self, market_id: str, start: str, end: str) -> list[dict]
def get_pm_universe(self, status: str = "ACTIVE") -> list[dict]
```

### 4.4 数据来源

| 数据 | SharedSignals 表 | 查询参数 | 频率 | 备注 |
|------|-----------------|----------|------|------|
| 市场列表 | `market_pm_markets` | — | 实时 | 所有活跃/已结算市场 |
| 价格 | `market_pm_prices` | `market_id, start, end` | 实时 | YES/NO 价格 (∈ [0, 1]) |
| 历史结算 | HuggingFace parquet | — | 离线 | `pm_historical_loader.py` |

**PM 特有因子** (旧系统 `pm_prediction_model.py` + `pm_nlp_model.py`):
- `implied_probability` — 市场隐含概率 (YES 价格)
- `volume_weighted_avg` — 成交量加权均价
- `bid_ask_spread` — 买卖价差
- `liquidity_depth` — 深度 (bid/ask 队列)
- `time_to_resolution` — 距结算天数
- `sentiment_score` — NLP 情绪分 (从描述/新闻)
- `model_probability` — 模型预测概率 (alpha 来源)
- `brier_score` — Brier 校准分 (历史)

### 4.5 建议策略清单 (从旧 PM 工具迁移)

旧系统 20 个工具:

| 策略名 | 类型 | 旧工具 | 说明 |
|--------|------|--------|------|
| `probability_arbitrage` | 概率套利 | `pm_shadow_runner.py` | 模型概率 vs 市场概率差距 → 买入被低估侧 |
| `event_driven` | 事件驱动 | `pm_prediction_model.py` | 事件发展更新后重估概率 |
| `kelly_sizing` | Kelly 仓位 | `pm_shadow_runner.py` (edge×kelly×hold) | Kelly 公式决定仓位大小 |
| `nlp_sentiment` | NLP 情绪 | `pm_nlp_model.py` | 文本特征 → 概率校正 |
| `early_exit` | 提前退出 | `pm_simulator.py` | 概率大幅移动后获利了结 (不等结算) |
| `calibration_arbitrage` | 校准套利 | `pm_forward_validation.py` (Brier) | 利用市场系统性定价偏差 |

**PM 特有维度** (替换六维打分):
- 传统六维 (macro/event/fundamental/capital/technical/sentiment) 大部分不适用
- 建议 PM 独立打分:
  - `probability_edge` (0.30) — 模型 vs 市场概率差
  - `information_timeliness` (0.20) — 信息新鲜度
  - `liquidity` (0.15) — 做市深度
  - `time_premium` (0.15) — 距结算时间 (越近越确定)
  - `calibration_quality` (0.10) — 历史校准 Brier 分
  - `sentiment` (0.10) — NLP 情绪分

### 4.6 Shadow 执行注意点

| 注意点 | 详情 |
|--------|------|
| **概率单位** | 盈亏以概率计 (0–1), 非价格; 买入 $0.60 的 YES, 结算为 $1 → 盈利 $0.40/合约 |
| **CLOB** | Polymarket CLOB (Central Limit Order Book); 影子盘假设 taker 成交价 = `market_pm_prices.last_price` |
| **提前退出** | 不等结算, 概率大幅移动即可退出; `pm_simulator.py` 已支持 `early_exit` |
| **结算风险** | 部分市场结算争议 (UMA 裁决); 影子盘记录 `resolution_status` |
| **Kelly 公式** | `f = (bp - q) / b`, 其中 `b = (1 - price) / price`, `p = model_prob`, `q = 1 - p`; 影子盘做 fractional Kelly (1/4 或 1/2) |
| **关联性** | 多个市场可能有共同底层事件 (如 "Trump wins" + "GOP wins Senate"); 影子盘需记录 topic correlation 避免过度集中 |
| **基准** | 等权持有所有活跃市场 (impossible in practice, 作为理论基准) 或 buy-and-hold 等概率组合 |
| **资金** | 初始 shadow 资金 50,000 USDC; 单市场 ≤5% ($2,500), 同时持仓 10–20 个市场 |
| **复盘** | Brier Score + P&L + calibration curve; 旧系统 `pm_forward_validation.py` 已实现 |

### 4.7 Reader 对接方式

```python
class PMAdapter:
    market = "pm"

    def __init__(self):
        self.reader = TradingAgentDataReader()
        # PM 需要额外的 reader 方法
        # self.reader.reader.get_pm_market(market_id)
        # self.reader.reader.get_pm_prices(market_id, start, end)

    # PM 不调用 get_bars_daily / get_factors (这些是股票/期货用)
    # 改用专用 PM 方法
```

---

## 5. 跨市场共通配置

### 5.1 影子盘执行日志路径

```
shared/data/shadow/
├── shadow_trades.jsonl       # ShadowTrade 记录 (共享, 按 market+capital_layer 字段区分)
├── shadow_positions.json     # position snapshots
├── shadow_pnl.json           # P&L snapshots
└── .shadow.lock              # file lock (SQLite 迁移前)
```

### 5.2 信号文件路径 (每市场独立)

```
signals/
├── pending/        # 待触发条件
├── filled/         # 已触发/已执行
├── cancelled/      # 已取消
└── positions/      # 当前持仓
```

### 5.3 复盘输出路径 (每市场独立)

```
{Market}/logs/
├── daily_reviews.jsonl
├── weekly_reviews.jsonl
├── monthly_reviews.jsonl
└── strategy_scorecards.jsonl
```

### 5.4 内存/记忆 (每市场独立)

```
{Market}/memory/
├── adversarial/
│   ├── bull/      # 多头 agent 记忆
│   └── bear/      # 空头 agent 记忆
├── positions/     # 持仓记忆
└── strategy_states/ # 策略状态记忆
```

### 5.5 市场特定 risk_limits.yaml 默认值

| 参数 | A股 | Crypto | US | PM |
|------|-----|--------|-----|-----|
| `single_stock_max` | 15% | 20% | 15% | 5% |
| `max_positions` | 5 | 10 | 10 | 20 |
| `stop_loss_pct` | -8% | -15% | -10% | 不适用 (用概率止损) |
| `trailing_stop_pct` | -12% | -25% | -15% | 不适用 |
| `drawdown.portfolio_max` | -10% | -25% | -15% | -10% |
| `black_swan.market_drop_pct` | -3% | -8% | -4% | 不适用 |
| `volatility_baseline` (年化) | 20% | 80% | 18% | 不适用 (概率波动) |
| `daily_loss_limit` | -3% | -5% | -3% | -2% |

### 5.6 策略晋级门槛 (统一)

| 阶段 | 最低 trades | fill_rate | positive_days | max_drawdown | avg_slippage |
|------|------------|-----------|---------------|--------------|--------------|
| research → shadow | ≥50 | ≥90% | — | — | ≤0.15% |
| shadow → sim | ≥100 | — | ≥60% | ≤10% | — |
| sim → real | ≥200 | ≥95% | ≥65% | ≤10% | ≤0.10% |

---

## 6. 实现优先级

按 `BATCH_PLAN_20260630.md` 的 Wave 结构:

| Wave | 内容 | 涉及本文档 |
|------|------|-----------|
| W1-1 | 数据流基线 + 多市场 `reader.py` 扩展 (PM 方法) | §4.7 |
| W2-1 | `MarketAdapter` 接口定型 + `capital_layer` 贯穿 | §0 |
| W2-2 | orchestrator 实现 (接各市场 adapter) | §0 |
| W4-1 | A股影子盘闭环 | §1 |
| W4-2 | Crypto 影子盘闭环 | §2 |
| W4-3 | US 影子盘闭环 | §3 |
| W4-4 | PM 影子盘闭环 | §4 |

---

## 7. 待确认事项

| # | 事项 | 影响 |
|---|------|------|
| 1 | `market_pm_markets` / `market_pm_prices` 实际列 schema? (本文档基于 `docs/data_contract.md` 推断) | PM adapter 实现 |
| 2 | SharedSignals 中 Crypto/US 的 `market` 列值是 `"Crypto"`/`"US"` 还是小写? | reader 查询参数 |
| 3 | 六维打分改造: 是注入 `market` 参数复用现有 scorer, 还是每市场独立 scorer? | W2-1 设计 |
| 4 | PM 独立打分维度 (替换六维) 是否接受? | PM adapter 设计 |
| 5 | `shadow_market_configs.md` 应存 `docs/` 还是各市场子目录? | 文件位置 |
