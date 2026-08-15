# Crypto 10 币 5m A 类候选因子预筛（非证据研究）

> **非证据声明**：本报告全部数字来自无 PIT 证明的 TradingDatas 历史回填
> 数据（`historical_backfill_no_pit=true`），仅供工程/定义检查
> （`not_promotion_evidence=true`、`authority=none`），
> **不得进入任何晋级证据**，不构成 edge、概率校准或参数变更授权。

> **模板说明**：本文件为结构模板。真实数字由服务器一次性跑批后经
> `python3 -m Crypto.ten_symbol_factor_prescreen --raw-dir <raw> --report
> <path>` 生成（同一渲染器、同一表结构），再由人工核对填入"结论与预注册
> 建议"。仓库内不联网；拉取模式需要显式注入 loopback transport 参数。

## 方法

- 数据：10 币（固定 `market_observation.OBSERVATION_SYMBOLS` 顺序）5m
  OHLCV，经 catalog/query 契约拉取并落盘为 canonical raw JSON（含
  receipt/data_through/observed_at 汇总）；行校验 UTC、OHLC 一致、
  Decimal；5 分钟缺口只记录不填补。
- 标签：forward 1h（12 槽）close→close；成本与证据链同一口径：fee
  0.001 双边 + slippage 2bps 双边（`crypto-round-trip-taker-v1`，
  `(1+net)*(1-slip)^2-1`）。
- 口径：每个候选同时报全样本与**非重叠子样本**（每 12 槽取 1），
  重叠标签对样本量的虚增在结果表中直接对比。
- 指标：signal/universe、hit_rate、mean/median net、vs always-invest
  基线、vs cash、等权权益曲线 max drawdown、turnover；per-symbol
  分解见各候选小节。

## 数据窗口

| symbol | rows | first_open_time | last_open_time | gaps |
|---|---|---|---|---|
| （待跑批填入，10 行） | | | | |

## 候选：XS-RS 横截面相对强弱（long top-k 等权，永远在场）

假设：cross-sectional relative strength: rank symbols by 1h return each
slot, long top-k equal weight, always in market。

| variant | signal/universe | hit_rate | mean_net | median_net | Δ baseline | Δ cash | maxDD | turnover | 非重叠 signal/slots | 非重叠 mean |
|---|---|---|---|---|---|---|---|---|---|---|
| top_1 | （待填） | | | | | | | | | |
| top_2 | | | | | | | | | | |
| top_3 | | | | | | | | | | |

per-symbol（top_2 入选统计）：

| symbol | 入选次数 | hit_rate | mean_net |
|---|---|---|---|
| （待填，10 行） | | | |

## 候选：短期反转（per-symbol 超跌企稳做多）

假设：long when 1h return <= -0.3% and 15m return > -0.1%；naive 版为
1h return < 0。

| variant | signal/universe | hit_rate | mean_net | median_net | Δ baseline | Δ cash | maxDD | turnover | 非重叠 signal/slots | 非重叠 mean |
|---|---|---|---|---|---|---|---|---|---|---|
| strict | （待填） | | | | | | | | | |
| naive | | | | | | | | | | |

per-symbol（strict / naive 各一张表，待填）。

## 候选：Amihud 非流动性（long top-2 高非流动性等权）

假设：rank symbols by |1h return| / 1h quote_volume each slot, long top-2
most illiquid equal weight。

| variant | signal/universe | hit_rate | mean_net | median_net | Δ baseline | Δ cash | maxDD | turnover | 非重叠 signal/slots | 非重叠 mean |
|---|---|---|---|---|---|---|---|---|---|---|
| top_2 | （待填） | | | | | | | | | |

per-symbol（入选统计，待填）。

## 候选：波动率 regime 修饰（time_series_momentum_v1 分高/低波动两半）

假设：evaluate the pre-registered time_series_momentum_v1 signal
（1h >= 0 and 15m >= 0.001）separately on the high and low
realized-1h-volatility halves。

| variant | signal/universe | hit_rate | mean_net | median_net | Δ baseline | Δ cash | maxDD | turnover | 非重叠 signal/slots | 非重叠 mean |
|---|---|---|---|---|---|---|---|---|---|---|
| high_vol_half | （待填） | | | | | | | | | |
| low_vol_half | | | | | | | | | | |

median realized_volatility_1h = （待填）

## 结论与预注册建议

（待填：基于上表数字的人工判断——哪些候选值得正式预注册进证据链。
注意全部结论仅在非重叠子样本口径下也成立时才考虑预注册；本报告
不构成任何晋级证据。）

---

生成：`Crypto/ten_symbol_factor_prescreen.py --report`；contract
`tradingagent.crypto.ten_symbol_factor_prescreen.v1`；cost policy
`crypto-round-trip-taker-v1`。
