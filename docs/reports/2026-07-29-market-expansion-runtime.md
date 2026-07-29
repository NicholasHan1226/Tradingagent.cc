# 2026-07-29 市场扩容与运行收口

本文只记录 2026-07-29 的新鲜运行证据。代码合并、服务器数据服务、
TradingAgent 模拟 runtime、历史回填和真实交易权限是不同状态。

## 结论

- Crypto 数据积累已进入自动运行：10 个币种、20 个行情/规则 dataset 均可经
  TradingDatas 正式 18083 catalog/query API 读取。
- Crypto 180 天 5 分钟回填已完成并与增量采集衔接，但历史 `observed_at`
  是采集时刻，不是历史 PIT 证据。
- Crypto TradingAgent 模拟核心当前暂停在失败关闭状态。阻塞是 query
  `as_of` 与 envelope receipt/data-through 的证据绑定，不是行情缺失，也不是
  资本或账本损坏。
- A股 TradingDatas 30 股数据链继续自动采集；A股 TradingAgent 当日因缺失
  `13:05` 快照停止在 `11:30`，没有跳过下一根成交价或补写订单。
- A股 500 股扩容仅取得隔离 `500/500` 单快照，生产数据面仍是 30 股。
- 所有市场保持 `REAL_TRADING_ENABLED=false`。

## Crypto 数据面

正式数据面：

- base URL：服务器 loopback `127.0.0.1:18083`
- catalog version：`v1-3326a9533d6beac2`
- 币种：BTC、ETH、SOL、XRP、BNB、DOGE、ADA、TRX、LINK、AVAX
- 每个币种一份 5 分钟行情 dataset 和一份规则 dataset
- 行情/规则 readback：20/20 `ready/fresh/valid/non-degraded`
- 自动行情 timer、规则 timer 与内部 API：均 `enabled/active`

180 天回填完成后的 SQLite 只读核验：

- 每个 5 分钟 dataset：51,844 行
- 最早 open time：`2026-01-30T07:35:00Z`
- 最新 open time：`2026-07-29T07:50:00Z`
- 每个币种重复 open time：0
- 每个币种非 300 秒时间间隔：0
- 每个规则 dataset：1 行
- SQLite `quick_check=ok`

回填通过现有通用 collector 和 receipt 路径完成，共 60 个时间窗口、10 个行情
dataset、600 次成功采集。回填期间暂停的增量窗口随后通过同一 collector 补齐，
再恢复自动 timer。该结果证明历史序列可用于离线研究，不证明历史首次可知时间或
实时执行时延。

## Crypto TradingAgent

代码状态：

- `origin/main=69e03e6bbfbfcfd2ee4541b471e106a67f7c8d1f`
- Crypto 离线学习已从 5 分钟核心拆出
- 学习失败不再是核心依赖
- 学习 timer 尚未部署或启用

运行状态：

- 服务器 TradingAgent `current` 仍为
  `bc8880dfd3c77ee358736d58e0cf9c377de154b3`
- runtime manifest 已更新到当前 Crypto catalog，四个 BTC/ETH 核心 dataset
  的合同哈希未变化
- Crypto 核心 timer 为 `enabled/inactive`
- 最后失败原因：
  `metadata.data_through must not be after the requested as_of`

TradingDatas 对本次 query 的 rows 已按 `as_of` 正确过滤，但 envelope 仍返回
该 dataset 全局最新 receipt/data-through。TradingAgent 不允许把未来 receipt
绑定到较早补跑槽，因此没有删除 backlog、跳过 observation 或打开新资本 generation。
修复应位于 TradingDatas 的通用 query evidence projection；TradingAgent 不放松
future/PIT gate。

## A股数据与模拟盘

数据面：

- 正式 18082 与 30 股通用采集 timer 均 `active/enabled`
- 2026-07-29 下午 `13:10–15:00` 每根均为 30/30
- `13:05` 精确 query 为零行

模拟盘：

- 当日最后成功处理 bar end：`11:30`
- 已处理快照数：10
- `pending` 仍包含 baseline 与 dynamic-position 两个 sleeve
- 下午失败原因在隔离状态副本中复现为
  `MinuteDataContractError: minute_query_returned_no_bars`
- 既有持仓、资本和订单没有回填、跳时或改写

由于 `13:05` 是 11:30 后的下一根可成交 K 线，系统不能用 `13:10` 代替它结算
既有 pending 动作。2026-07-29 因此是非完整、不可学习交易日。下一交易日从新的
session initializer 和第一根完整 K 线重新开始。

## A股 500 股候选

隔离 clean store 已完成一次：

- `security_master`：5,952 行
- `rt_min`：500 行、500 个唯一股票、同一 bar end
- 5 个 100 股票分片全部成功
- Universe SHA256：
  `1d4f2aa824c2dcb82ee3ba1b39f544f9c38bcb72033ae0804f90d8f428db716f`
- 500 个股票均满足主板、上市状态 L、CNY 和上市满 30 日条件

该证据不是生产切换。下一门禁是交易时段内两个相邻 bar end 均取得
`500/500`，并验证单轮耗时、receipt 聚合和失败降级；此前继续使用 30 股生产链。

## 下一停止线

1. TradingDatas 修复并正式证明 query `as_of` 与 envelope receipt/lineage/
   data-through 绑定后，Crypto TA 先 one-shot，再验证同槽幂等和两个相邻自动轮。
2. Crypto 核心连续运行 24 小时且零重复成交、零账实差异后，才部署独立学习 timer。
3. 2026-07-30 A股从新会话验证 30 股完整分钟闭环；不补写 2026-07-29 缺失槽。
4. A股 500 股取得两根相邻 live `500/500` 后，再决定是否替换 30 股数据 cohort。
5. DeepSeek 继续只作为离线新闻、事件和产业证据侧车，无候选、仓位、风险或订单权限。
