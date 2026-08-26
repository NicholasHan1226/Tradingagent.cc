# 加密高波动门控动量（K10）前向确认预注册（research_only）

- 制定日期：2026-08-26。性质：冻结预注册，先于任何 K10 新数据分桶收益
  读数合入主线。绑定：候选台账 K10（2026-08-26 可行性探针归档 #578 晋级
  「可预登记」）。
- 机制假设：24h 动量的可交易性由波动状态门控——高波动三分位内动量延续，
  中低波动区动量失效或反转。与既有动量族全灭证据互洽（毒在低波区）。
  多头臂只需现货 delayed-paper 核心，无需永续腿/maker 数据面；对 #33 三选
  项的证据格局是增量信息，不改变其前置结构。
- 数据依赖：`tradingdatas-crypto` read model `crypto.spot.binance.{sym}.5m`
  持续采集（watch 面板在位，ok=11767 fail=1 / 24h @ 2026-08-26）。backfill
  无 PIT 证明。

## 为什么是前向确认

可行性初筛与确认通道都已看过全部可得 backfill 历史（~188 天，至
2026-08-25），样本内不存在干净的 holdout 分割。唯一无污染确认 = 本文档冻
结后，只对冻结日之后新采集的数据做一次性读数。

## 冻结定义 D1–D4

**D1 宇宙与信号**：宇宙冻结为冻结日（2026-08-26 UTC）在
`crypto.spot.binance.%.5m` 覆盖 ≥54,000 根的现货符号，共 40 个（清单见文
末附录）；冻结日后新上市符号不进入。信号日频非重叠 stride=24h、00:00 UTC
网格：

- `mom = ln(close_t / close_{t-24h})`，两根 close 均须存在且各自窗口内
  连续 5m 根数 ≥250；
- realized vol = 截至 t 的最近 288 根连续 5m 对数收益标准差；
- 高波边界 = 该符号全部严格早于 t 的小时 vol 样本的 2/3 分位（走前
  expanding，无 look-ahead），样本 <720 个小时点（~30 天）不激活。

**D2 臂与主假设**：

- **H1（主，唯一门柱判定对象）**：mom>0 且 vol≥p66 → 持有现货 24h，
  收益=未来 24h 现货对数收益。机制绑定：高波动状态下的动量延续。
- H2（次级描述性）：mom<0 且 vol≥p66 → aligned=-fwd。当前基础设施无现
  货空头执行面，H2 只作描述性对照，不做门柱判定、不进观察名单；若未来要
  用，须独立重新预注册。

**D3 成本与成交语义**：正式引擎接链内已记录的 taker 成本模型 +
delayed-paper 成交语义；任何中间读数用 flat 12bps 往返示意净口径并标注
`not_promotion_evidence=true`。

**D4 判定冻结**：首次前向读数为一次性。H1 进观察名单门槛（四条同时满
足）：净均值>0；胜率>0.5；合并 n≥30；逐符号正均值占比 ≥60%（只统计事
件数 n≥20 的符号）。任一不足 → K10 关停（台账行翻转为「探针晋级 + 前向
确认失败」），不得因擦线重试、不得再参数化后追认。

**再参数化条款**：stride、分位边界、vol/vol 窗口、成本口径、宇宙清单任
一改动即本预注册作废，须重新文档化并重新计时累积，原累积数据不得复用为
新预注册的确认样本。

## 读数时点冻结

- 累积起点：2026-08-26 UTC 之后的新采集数据。
- 默认读数日：2026-11-26（UTC 数据日），一次性执行。
- 届时合并 n<30 → 顺延至 2027-02-26 复读一次；两次合计仍 n<30 → 按
  数据面异常处理，不产生门柱判定，单独归因报告说明覆盖缺口原因。
- 引擎 PR 在本 PR 合入之后提交；读数前不跑任何 K10 分桶收益计算（含中
  间探针、滚动监控变体）。

## 覆盖率记录格式（待读数填写）

| 口径 | n | 净均值 bps | 胜率 | 逐符号正均值占比 |
|---|---|---|---|---|
| H1 全合并 | 待证据 | 待证据 | 待证据 | 待证据 |
| H1 分臂 mom_up×hivol | 待证据 | 待证据 | 待证据 | — |

## 多重比较纪律

K10 是 2026-08-26 当日多格筛查中的幸存者，属 screening hit：本预注册合入
后计入「备选」，不计入「已考核」，直至前向确认读数通过 D4 门柱。无论读
数结果如何均不作部署候选；策略化需独立预注册正式引擎网格。

## 附录：冻结宇宙清单（40 符号）

aaveusdt adausdt aptusdt arbusdt atomusdt avaxusdt bchusdt bnbusdt
btcusdt crvusdt dogeusdt dotusdt enausdt etcusdt ethusdt fetusdt filusdt
grtusdt hbarusdt injusdt jupusdt ldousdt linkusdt ltcusdt nearusdt
ondousdt opusdt polusdt pythusdt renderusdt seiusdt solusdt strkusdt
suiusdt tiausdt trxusdt uniusdt wldusdt xlmusdt xrpusdt

## 时点声明

本文档先于任何 K10 新数据收益读数合入主线；引擎 PR 随后提交；一次性读
数按上述时点执行并落台账行变更。
