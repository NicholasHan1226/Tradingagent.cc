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

## 20:32 A股缺口恢复代码收口

- PR [#76](https://github.com/NicholasHan1226/Tradingagent.cc/pull/76) 已普通
  合并为 `cacb1b1a675665987c8e6d7243377a633f31a23c`；GitHub front/test CI
  通过，本地 A股全量 `840 passed`。
- 修复不会改写上述 2026-07-29 历史 bundle，也不会补造 `13:05`。
- 后续同类事故会把所有跨缺口 pending 模拟订单记为未成交，记录精确缺口，
  重置滚动特征；恢复第一根完整 K 线只建立基线，下一根连续完整 K 线才重新产生
  候选。
- 缺口日始终不能进入全天完整性或离线学习验收，但后续完整 K 线可以继续积累
  observation、反事实、盯市和对账证据，避免一次缺口使全天永久停止。
- 服务器已从 `bc8880dfd3c77ee358736d58e0cf9c377de154b3` 原子切换到
  `946db638c9ac85410fa697f81dd1c6da02723903`；新 release 的 824 个文件树哈希
  与本地归档一致，现役解释器模块导入及 systemd unit verify 通过。原两个 A股
  timer 保持 `enabled/active`，未新增任务；旧 release 保留为直接回滚。

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

## 17:00 盘后增量

- 16:30 TradingDatas 通用盘后轮 `success/exit0`。
- 正式 18082 的 `cn.equity.daily(20260729)` 已由 TA UID987 读回为
  `ready/success/fresh/valid/non-degraded`，receipt 与 lineage 完整。
- 当前审核 30 股通过 3 个每批 10 只的有界查询取得 30/30，跨批 30 个唯一
  `ts_code`，双跑一致。
- `cn.market.trade_calendar(exchange=SSE,cal_date=20260730)` 仍返回 0 行。
  现役合同不允许覆盖未来日期，因此没有用 direct shell、手工 SQLite、专用 route
  或第二 service 绕过。TradingDatas 正在独立评审由 registry 明确声明
  `known_future_horizon_days=1` 的通用修正。
- TA 的隔离明日会话预检仍失败关闭；正式 `20260730` 状态目录不存在，未产生
  state bundle、资本、订单或成交。

## 17:58 次日会话预检收口

- TradingDatas 的通用下一日日历合同已通过独立 clean-overlay review：
  248 个相关测试通过，P0/P1=0。正式不可变 release 为
  `64695852ff5be23b3cf8a8d1d03a13f7274e4586`，旧
  `5ac3925c3931a81132ea02abb16f9745033fb6dc` 保留为回滚点。
- 17:50 通用采集完成后，正式 18082 以 TA UID987 查询
  `cn.market.trade_calendar(exchange=SSE,cal_date=20260730)` 得到唯一行：
  `is_open=1,pretrade_date=20260729`。metadata 为
  `ready/success/fresh/valid/non-degraded`，receipt 与 provider-neutral
  lineage 完整。
- 初始化第一次尝试时 `cn.equity.daily` 尚未完成 17:55 盘后刷新，系统按
  `minute_session_dataset_rejected:cn.equity.daily` 失败关闭，没有生成输入。
  刷新完成后，30 股日线经三批 10 只有界查询取得 30/30，三批均
  `ready/fresh/valid/non-degraded`。
- 隔离 initializer 随后双跑通过：首次 `reused=false`，第二次
  `reused=true`；`symbol_count=30`，Universe SHA256 为
  `0e26f54fc2ab391f0187a5787f9955b90e8a2ff21969957565749b733e035203`。
  目标目录只有 3 项 0600 输入，无 state bundle、资本、订单、成交或账本；
  正式 `20260730` 状态目录继续不存在，等待 09:18 timer 独立运行。

## 下一停止线

1. TradingDatas 修复并正式证明 query `as_of` 与 envelope receipt/lineage/
   data-through 绑定后，Crypto TA 先 one-shot，再验证同槽幂等和两个相邻自动轮。
2. Crypto 核心连续运行 24 小时且零重复成交、零账实差异后，才部署独立学习 timer。
3. 2026-07-30 A股从新会话验证 30 股完整分钟闭环；不补写 2026-07-29 缺失槽。
4. A股 500 股取得两根相邻 live `500/500` 后，再决定是否替换 30 股数据 cohort。
5. DeepSeek 继续只作为离线新闻、事件和产业证据侧车，无候选、仓位、风险或订单权限。
