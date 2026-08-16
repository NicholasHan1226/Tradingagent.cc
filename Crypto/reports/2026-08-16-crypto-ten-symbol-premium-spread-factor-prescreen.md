# Crypto 10 币 premium+spread 族候选因子预筛（非证据研究）

> **非证据声明**：本报告全部数字来自无 PIT 证明的 TradingDatas 历史回填数据
>（`historical_backfill_no_pit=true`），仅供工程/定义检查
>（`not_promotion_evidence=true`、`authority=none`、`research_only=true`），
>**不得进入任何晋级证据**，不构成 edge、概率校准或参数变更授权。

## 方法

- 数据：10 币（固定 `market_observation.OBSERVATION_SYMBOLS` 顺序）5m OHLCV
  bar + 同币 perp premium-index 5m 序列（`crypto.perp.binance.<symbol>.premium_index`，
  字段 `close` 即 funding proxy level，日度 metrics dump 落库、每币 288 行/日的
  5m 网格）。
- 对齐：每币取 bar `open_time` 与 premium `open_time` 的交集，再取 10 币共同可得的
  保守窗口（见数据窗口表），跨缺口槽由窗口连续性检查剔除。
- **数据来源治理说明（重要）**：本轮生产 18083 对 `.premium_index` 的 query 因
  `transport_profile_unverified` fail-closed 返回空（`degraded=true`、
  `freshness.state=failed`）。为产出预筛结论，本轮的 bar 与 premium 序列均来自服务器
  TradingDatas crypto read-model SQLite 的**只读诊断抽取**
  （`premium_source=sqlite_readonly_diagnostic`）。这是研究 only 的一次性绕过，
  **不构成 Crypto 消费 SQLite 的代码路径**；`Crypto/ten_symbol_premium_spread_prescreen.py`
  的正式 `--fetch` 路径只走 `GET /v1/catalog` + `POST /v1/query`（绝不 import
  `sqlite3` 或读 TradingDatas 数据库），待 18083 对 premium dataset 的 release
  门禁恢复后应重跑复算。
- **spread 数据治理说明（重要）**：`spread_regime` 族的 regime 值来自实测点差投影
  artifact 的 per-symbol 最新充足 UTC 日 bucket 的 `p75_bps`（半点差，bps）。该
  artifact **目前只有 `2026-08-15` 一天有数据**（22 槽、每 symbol 17-18 样本，
  BTCUSDT 18 样本）。即本报告的 spread regime 是**单日静态值**套到整个 197 天
  历史窗口——这是该族的冻结定义（static per-symbol 流动性分区），但单日 p75 对
  历史窗口的代表性有限，结论仅能证明"这套 gate 定义能跑通"，不能证明 regime
  门有真实 edge。缺失 symbol/日期的按无充分样本处理（本报告 10 币均有单日数据）。
- 候选定义：直接展开 stage-2 假设生成器冻结网格
  `crypto-ten-symbol-hypothesis-generation-v1` 的两族 B 类因子
  （`premium_momentum`、`spread_regime`），共 8 候选。
- `spread_regime` 复用 frozen signal `time_series_momentum_v1`
  （`return_1h >= 0` 且 `return_15m >= 0.001`，来自 `Crypto.factor_research._signal`
  与 `ten_symbol_factor_prescreen` 同源的 snapshot 构造）；其 always-invest 基线
  为 gate 后 universe 的等权 always-invest（即"在同样 regime 内不做 signal 择时"）。
- 标签：forward 60/240/720/1440min（12/48/144/288 槽）close→close；成本与证据链
  同一口径：fee 0.001 双边 + slippage 2bps 双边，`(1+net)*(1-slip)^2-1`
  （cost policy `crypto-round-trip-taker-v1`，往返约 0.24%）。
- 口径：每个候选×horizon 报全样本与非重叠子样本（stride=horizon 槽数），重叠
  标签对样本量的虚增在表内直接对照。
- 指标：signal/universe、hit_rate、mean gross（费用前）/mean net（费用后）、
  vs always-invest 基线；非重叠子样本的 gross/net 并列。

## 数据窗口（10 币共同保守窗口）

| symbol | bars | premium | first_open_time | last_open_time | gaps | premium_source |
|---|---|---|---|---|---|---|
| ADAUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| AVAXUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| BNBUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| BTCUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| DOGEUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| ETHUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| LINKUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| SOLUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| TRXUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |
| XRPUSDT | 56741 | 57024 | 2026-01-30T07:35:00Z | 2026-08-15T23:55:00Z | 192 | sqlite_readonly_diagnostic |

对齐窗口 2026-01-30T07:35Z → 2026-08-15T23:55Z（约 197 天）。`bars` 列是
bar∩premium 对齐后的共同 5m 槽数（56741）；`premium` 列是该 symbol 的原始 premium
行数（57024 = 198 天×288）。`gaps=192` 为各 symbol 序列内缺失 5m 槽数（含
08-05→06 的 10 币共享缺口及 bar/premium 边界不对齐），跨缺口窗口一律剔除。

## spread regime 覆盖（单日 p75 半点差，bps）

| symbol | p75_bps | sample_count | slot_count |
|---|---|---|---|
| ADAUSDT | 5.60067208 | 17 | 22 |
| AVAXUSDT | 1.53574445 | 17 | 22 |
| BNBUSDT | 0.16356306 | 17 | 22 |
| BTCUSDT | 0.00158487 | 18 | 22 |
| DOGEUSDT | 1.42785750 | 17 | 22 |
| ETHUSDT | 0.05305617 | 17 | 22 |
| LINKUSDT | 1.05301953 | 17 | 22 |
| SOLUSDT | 1.32459103 | 17 | 22 |
| TRXUSDT | 3.01795684 | 17 | 22 |
| XRPUSDT | 0.99775505 | 17 | 22 |

依据该单日 p75 静态分区：`narrow_only` 1.0bps 只通过 BTC/ETH/BNB/XRP（4 币），
1.5bps 再加 DOGE/LINK/SOL（7 币），2.0bps 再加 AVAX（8 币，仅排除 ADA/TRX）；
`wide_only` 1.5bps 只通过 ADA/TRX/AVAX（3 币）。**注意该 p75 是 2026-08-15 单日
样本，且 ADA（5.60）与 BTC（0.0016）相差约 3500 倍，套到历史窗口只是冻结定义的
静态分区，不构成该 symbol 历史区间的真实逐日 spread 证据。**

## 结论摘要（8 候选 × 4 horizon）

| candidate_id | 族 | 最优全样本 net | 最优非重叠 net | 4 horizon 全负 | 判定 |
|---|---|---|---|---|---|
| spread_regime__t1p0_narrow | spread_regime | -0.2532% | -0.1146% | 是 | 否决 |
| spread_regime__t1p5_narrow | spread_regime | -0.2546% | -0.1142% | 是 | 否决 |
| spread_regime__t2p0_narrow | spread_regime | -0.2544% | -0.1350% | 是 | 否决 |
| spread_regime__t1p5_wide | spread_regime | -0.2530% | -0.0346% | 是 | 否决 |
| premium_momentum__l12_t0p0005 | premium_momentum | -0.2145% | -0.2018% | 是 | 否决 |
| premium_momentum__l48_t0p001 | premium_momentum | -0.1244% | +0.1988%（n=8） | 是 | 否决 |
| premium_momentum__l288_t0p002 | premium_momentum | -0.0691% | +0.3839%（n=4） | 是 | 否决 |
| premium_momentum__l48_tm0p001 | premium_momentum | -0.2458% | -0.2457% | 是 | 否决 |

**8 个候选在 4 个 horizon 上全样本费用后全部为负，本轮全部否决，不建议预注册任何
premium/spread 族假设。**（"最优"为跨 horizon 取最大 net；`l48_t0p001`/`l288_t0p002`
的非重叠最优为小样本正数但 n=8/n=4，统计上不可区分于零，详见统计效力声明。）

## 逐候选表

### 族 spread_regime（静态 spread regime gate × frozen `time_series_momentum_v1`）

| candidate | horizon | signal/universe | hit_rate | mean_gross | mean_net | Δ baseline | 非重叠 n | 非重叠 gross | 非重叠 net |
|---|---|---|---|---|---|---|---|---|---|
| t1p0_narrow | 1h | 43537 / 226716 | 24.8363% | -0.0135% | -0.2532% | -0.0070% | 3851 | -0.0327% | -0.2724% |
| t1p0_narrow | 240min | 43489 / 226428 | 34.3374% | -0.0275% | -0.2671% | -0.0014% | 955 | -0.0723% | -0.3119% |
| t1p0_narrow | 720min | 43435 / 225716 | 39.3024% | -0.0962% | -0.3357% | -0.0172% | 318 | +0.1254% | -0.1146% |
| t1p0_narrow | 1440min | 43361 / 224956 | 42.3906% | -0.1862% | -0.4254% | -0.0295% | 170 | +0.0393% | -0.2005% |
| t1p5_narrow | 1h | 83238 / 396753 | 26.3990% | -0.0149% | -0.2546% | -0.0091% | 7257 | -0.0303% | -0.2699% |
| t1p5_narrow | 240min | 83155 / 396249 | 35.5517% | -0.0237% | -0.2633% | -0.0002% | 1858 | -0.0746% | -0.3142% |
| t1p5_narrow | 720min | 83004 / 395003 | 40.5294% | -0.0798% | -0.3193% | -0.0082% | 606 | +0.1258% | -0.1142% |
| t1p5_narrow | 1440min | 82855 / 393673 | 43.2527% | -0.1762% | -0.4154% | -0.0323% | 313 | +0.0452% | -0.1947% |
| t2p0_narrow | 1h | 97996 / 453432 | 26.6735% | -0.0147% | -0.2544% | -0.0085% | 8527 | -0.0297% | -0.2694% |
| t2p0_narrow | 240min | 97905 / 452856 | 35.4895% | -0.0244% | -0.2641% | +0.0001% | 2182 | -0.0696% | -0.3092% |
| t2p0_narrow | 720min | 97726 / 451432 | 40.5890% | -0.0810% | -0.3205% | -0.0066% | 706 | +0.1050% | -0.1350% |
| t2p0_narrow | 1440min | 97548 / 449912 | 43.1726% | -0.1838% | -0.4231% | -0.0344% | 366 | -0.0046% | -0.2443% |
| t1p5_wide | 1h | 33459 / 170037 | 26.9554% | -0.0133% | -0.2530% | -0.0082% | 2860 | -0.0264% | -0.2660% |
| t1p5_wide | 240min | 33443 / 169821 | 35.2211% | -0.0228% | -0.2625% | -0.0034% | 724 | -0.0437% | -0.2833% |
| t1p5_wide | 720min | 33349 / 169287 | 40.3970% | -0.0764% | -0.3159% | -0.0151% | 233 | +0.2056% | -0.0346% |
| t1p5_wide | 1440min | 33279 / 168717 | 41.7621% | -0.2044% | -0.4436% | -0.0825% | 126 | -0.0934% | -0.3329% |

### 族 premium_momentum（premium level 差值 ≥ 阈值做多；负阈值 = fade 极端负 premium）

| candidate | horizon | signal/universe | hit_rate | mean_gross | mean_net | Δ baseline | 非重叠 n | 非重叠 gross | 非重叠 net |
|---|---|---|---|---|---|---|---|---|---|
| l12_t0p0005 | 1h | 21766 / 566790 | 30.9428% | +0.0253% | -0.2145% | +0.0307% | 1849 | +0.0380% | -0.2018% |
| l12_t0p0005 | 240min | 21761 / 566070 | 38.7942% | +0.0021% | -0.2376% | +0.0243% | 443 | -0.0925% | -0.3320% |
| l12_t0p0005 | 720min | 21741 / 564290 | 42.1370% | -0.0400% | -0.2797% | +0.0283% | 139 | -0.6652% | -0.9034% |
| l12_t0p0005 | 1440min | 21717 / 562390 | 44.4767% | -0.0870% | -0.3266% | +0.0500% | 62 | -0.9123% | -1.1498% |
| l48_t0p001 | 1h | 2593 / 566050 | 31.9321% | +0.0490% | -0.1908% | +0.0545% | 228 | -0.0075% | -0.2472% |
| l48_t0p001 | 240min | 2593 / 565350 | 38.5268% | +0.0308% | -0.2090% | +0.0534% | 48 | -0.2535% | -0.4926% |
| l48_t0p001 | 720min | 2593 / 563570 | 39.4909% | +0.0568% | -0.1830% | +0.1257% | 8 | +0.4395% | +0.1988% |
| l48_t0p001 | 1440min | 2592 / 561670 | 45.0231% | +0.1156% | -0.1244% | +0.2518% | 0 | — | — |
| l288_t0p002 | 1h | 235 / 562130 | 18.7234% | -0.0335% | -0.2731% | -0.0282% | 15 | +0.1095% | -0.1304% |
| l288_t0p002 | 240min | 235 / 561410 | 36.1702% | +0.1192% | -0.1208% | +0.1398% | 4 | +0.6252% | +0.3839% |
| l288_t0p002 | 720min | 235 / 559630 | 43.4043% | -0.0515% | -0.2911% | +0.0060% | 0 | — | — |
| l288_t0p002 | 1440min | 235 / 557730 | 60.0000% | +0.1710% | -0.0691% | +0.2783% | 0 | — | — |
| l48_tm0p001 | 1h | 563471 / 566050 | 24.9992% | -0.0061% | -0.2458% | -0.0005% | 46953 | -0.0060% | -0.2457% |
| l48_tm0p001 | 240min | 562772 / 565350 | 34.9772% | -0.0234% | -0.2630% | -0.0006% | 11724 | -0.0219% | -0.2616% |
| l48_tm0p001 | 720min | 560992 / 563570 | 41.1102% | -0.0702% | -0.3098% | -0.0010% | 3899 | -0.0745% | -0.3141% |
| l48_tm0p001 | 1440min | 559093 / 561670 | 43.6734% | -0.1392% | -0.3786% | -0.0024% | 1946 | -0.1530% | -0.3924% |

## 与 OI 三族及 OHLCV 四候选对比

- OHLCV-only 四候选（`xs_rs`/`short_reversal`/`amihud_illiquidity`/
  `momentum_vol_regime`）与 OI 三族 15 候选在 1h→24h 全频段费用后均为负，结论是
  "1h 特征族无 gross edge，成本吃光"。
- 本轮 premium/spread 两族 8 候选结论**与 OI/OHLCV 族同构且一致**：
  - `spread_regime` 四候选在 gate 内评估 frozen `time_series_momentum_v1`，hit_rate
    随 horizon 从 ~25% 升到 ~42%（成本占比被摊薄），但 gross 没有相应转正，
    费用后全为负；720/1440min 的非重叠 gross 偶有正（+0.04%~+0.21%），但费用后
    net 仍全负。
  - `premium_momentum` 四个候选里 `l12`/`l48` 在 1h/240min 的 gross 微正
    （+0.002%~+0.12%），但费用后全部转负；`l48_t0p001`/`l288_t0p002` 的非重叠
    720/240min 出现 +0.20%/+0.38% 的正 net，但对应 n=8/n=4，完全落在噪声区。
  - 唯一接近零的非重叠 net 是 `spread_regime__t1p5_wide` 720min（-0.0346%，
    n=233），仍为负且幅度远小于任何可证效应。
- 结论：引入 premium（funding proxy）与 spread regime（实测点差分区）这两类 B 类
  信息，没有带来可覆盖 0.24% 往返成本的毛 edge；与 OI 报告"约束不是只有成本，而是
  这些特征在长尺度上没有预测力"完全同构。

## 统计效力声明

- 非重叠独立样本量（stride=horizon 槽数）：
  - `spread_regime`（gate 后 universe 上做 signal 择时）：1h n≈2860-8527，
    240min n≈724-2182，720min n≈233-706，1440min n≈126-366。
  - `premium_momentum__l12_t0p0005`：1h n=1849，1440min n=62。
  - `premium_momentum__l48_t0p001`：720min n=8，1440min n=0（`l48_t0p001` 非重叠
    24h 无样本）。
  - `premium_momentum__l288_t0p002`：240min n=4，720/1440min n=0。
  - `premium_momentum__l48_tm0p001`：n 充足（1h n=46953，1440min n=1946），但
    该变体阈值 -0.001 几乎全槽触发，等于"无择时"的 always-invest，无增量。
- `l48_t0p001` 720min 非重叠 +0.1988%（n=8）与 `l288_t0p002` 240min 非重叠
  +0.3839%（n=4）**统计上完全不可区分于零**：沿用 24h 单槽 net 标准差约 2.95% 的
  历史估计，n=8 时标准误约 1.04%、n=4 时约 1.48%，远大于其均值本身；这是极低
  coverage（signal/universe≈0.5% 与 0.04%）造成的偶然正样本，不构成预注册理由。
- 全样本口径的重叠标签把名义样本量虚增约 12/48/144/288 倍，有效样本量与非重叠
  口径同阶；两口径并列即为此目的。
- 未做 HAC/多重比较校正：8 候选 × 4 horizon 共 32 个单元，未校正的多重比较会让
  噪声单元更容易假阳性；本报告全部数字为无 PIT 历史回填，只允许用于工程/定义
  检查，永不得进入晋级证据。

## 结论与下一步

**premium/spread 两族 8 候选在 4 个 horizon 上全部被否决，本轮不建议预注册任何
premium_momentum 或 spread_regime 假设。**

1. 引入 premium（funding proxy）与 spread regime（实测点差静态分区）没有带来可
   覆盖 0.24% 往返成本的毛 edge；两族在 1h→24h 全频段与 OHLCV/OI 族同构，都是
   "特征无预测力 + 成本吃光"。
2. 相对最不死的仍是 `premium_momentum__l288_t0p002`（全样本 24h net -0.069%，
   Δ baseline +0.278%）与 `premium_momentum__l48_t0p001`（非重叠 720min net
   +0.199%、n=8），但全样本仍为负、非重叠样本量极小，只是噪声排序。
3. **数据面阻塞（两条，均须在 release 恢复后复算）**：
   - 本轮因 18083 对 `.premium_index` query 的 `transport_profile_unverified`
     fail-closed 而改用 SQLite 只读抽取（`premium_source=sqlite_readonly_diagnostic`）。
     待 TradingDatas crypto release 恢复 premium query 后，应重跑
     `Crypto/ten_symbol_premium_spread_prescreen.py --fetch --report` 复算。
   - `spread_regime` 的 regime 值目前只有 `2026-08-15` 单日 p75（每 symbol 17-18
     样本），套到 197 天历史窗口是冻结定义的静态分区，但代表性有限；应等 spread
     投影累积更多充足 UTC 日后再复算，且若某些 horizon 因单日覆盖无法产出有效
     评估，应照实标注"insufficient realized_spreads evidence"而非沿用单日 p75。
4. premium 单 plane 未证 edge，不建议继续在 premium_momentum 族内换 lookback/阈值；
   spread_regime 门在单日覆盖下也未证 edge。下一轮更值得做的是等 premium/spread
   的正式 release 与多日 spread 覆盖就绪后复算，而不是在当前数据面上继续换参数。

---

生成：`Crypto/ten_symbol_premium_spread_prescreen.py --report`；contract
`tradingagent.crypto.ten_symbol_premium_spread_prescreen.v1`；cost policy
`crypto-round-trip-taker-v1`。机器结果见服务器
`/tmp/premium-spread-prescreen-20260816/premium-spread-prescreen-result.json`
（离线可复算；SQLite 只读抽取与 spread artifact 读取为一次性 driver，未进入
committed 代码路径）。
