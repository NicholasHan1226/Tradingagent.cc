# CNFutures × TradingDatas 最小数据交接合同

> 状态：**未交接，不可运行真实模拟**。本文件是 CNFutures 的消费者验收规格；它不新增 TradingDatas 公共路由、采集器、定时任务或交易语义。

## 目的与范围

唯一可运行策略 `commodity_intraday_trend` 仅需要豆粕 `M` 的日盘 5 分钟研究数据。交接必须经 TradingDatas 的既有 `GET /v1/catalog` 与 `POST /v1/query` 完成；CNFutures 不直读 SQLite、不能调用上游，也不能依据 catalog 条目或 HTTP 200 认定可用。

`RB` 不是本次交接的执行范围。它将来只能另行以只读影子研究合同接入，且不得影响 M 的策略、资金、订单或成熟度。

## 最小数据集语义

TradingDatas owner 可选择符合其 provider-neutral registry 的最终 dataset ID；CNFutures 不固化或臆测 ID。交接中必须逐项给出最终 ID、schema major、查询 filter/sort、catalog version 和样例 receipt/lineage。

| 语义 | 必需字段或证据 | 说明 |
| --- | --- | --- |
| 可交易合约主数据 | contract symbol、product=`M`、exchange、list/delist 状态 | 只允许明确合约，不能使用抽象连续代码下单。 |
| 日盘 5 分钟 bars | contract symbol、bar end（含时区）、OHLC、volume、trade date | 每一查询行均须可追溯到 receipt/lineage；不消费未来可见 bar。 |
| 交易日历与日盘会话 | trade date、calendar eligibility、session、available-at | 午休、休市、收盘前 flatten 和换月均按此门禁。 |
| 合约交易规格 | multiplier、tick、涨跌限制及适用日期 | 缺任一规格只允许 observation/hold，不生成 simulated fill。 |

保证金、手续费、生产端点、客户级限额与程序化报备不是 TradingDatas 的事实源；它们仍须分别经期货公司书面准入和未来 CTP/仿真测试确认，见 [REAL_BROKER_ADMISSION.md](REAL_BROKER_ADMISSION.md)。

## 消费前验收

每项验收都以同一时刻的 catalog 与 query envelope 为准：

1. catalog 中最终 dataset contract 可发现；这一步只确认合同，不确认数据可消费。
2. 对同一明确 M 合约读取两个相邻的、完整 5 分钟 bar；每根必须来自同一有效 trade date，且 bar end 连续、无重复、无未来可见数据。
3. 两次 query envelope 均满足 `ready`、`fresh`、`valid`、`degraded=false`，且有非空 receipt 与 lineage；任何一项失败则只保留 hold/observation。
4. 查询该会话对应的 calendar 和规格，并证明它们在策略 decision time 前可用；不能以静态 fixture、旧 bar 或消费者缓存替代。
5. 使用对方给出的精确 filter/sort/cursor 进行可终止分页 readback；最终 row identity、时间与 metadata 在幂等重读中一致。
6. 将上述 readback 保存为一次 CNFutures handoff receipt。通过后才可建立 `delayed-paper` session；它不构成 SimNow、CTP、券商生产接入或实盘授权。

## 当前只读发现（2026-07-30）

已查到 registry 中 `cn.dataset.fut_basic` 可执行，候选分钟数据 `cn.dataset.ft_mins` 仍为 paused/blocked（缺基础 seed receipt），`cn.dataset.rt_fut_min` 仍为 paused/locked。它们不是本合同指定的最终 dataset ID，也不满足上述验收。因此当前结果为 **NO-GO：不启动 CNFutures runtime、模拟成交或 scheduler**。

## 交接后的顺序

1. TradingDatas owner 完成有界、只读 API readback，并提供最终合同与 receipt/lineage。
2. CNFutures 在隔离 root 用 M 单策略运行 delayed-paper；首轮只做 observation、hold 与风险拒绝验证。
3. 覆盖正常、缺数据、午休、收盘前、换月和波动状态后，才保存完整模拟回合与前向标签。
4. RB 影子研究、SimNow/CTP 测试和券商生产外接均为后续独立门禁，不能与此交接合并。
