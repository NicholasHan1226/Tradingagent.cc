# Crypto 40 币观察身份修复自然读回（只读验收）

> 只读读回记录：未改变任何 timer、authority、数据或配置；全部证据来自生产
> append-only 事件日志与运行中服务的自然产物（`authority=none`、
> `real_trading_enabled=false`）。本文不构成晋级证据。

## 结论摘要

1. **身份修复已验证生效**：40 币观察事件日志中的 ten-symbol 继承前缀事件止于
   观察截止 `2026-08-22T11:20:55Z`；此后首条事件起所有 event_id 均为
   `crypto-forty-*` 且 contract 均为 forty 专用契约，切换干净、无交叠。
   部署在服务器 current 的发布包含该隔离修复。
2. **新发现（开放缺陷）**：修复部署后，40 币观察器从未产出过一次成功观察——
   修复后事件共 207 条，全部为 `data_reject`（121）或 `data_gap`（86）；
   全日志唯一一条 `observation` 类型事件正是部署前那条继承身份的旧事件。
   同期十币观察器持续成功（累计 1510 条成功观察，最新成功观察截止
   `2026-08-23T04:00:55Z`），证明共享数据面新鲜度正常，失败为 40 币通道特有。

## 身份切换证据

全日志 208 条事件、两个身份族：

| 事实 | 数值 |
|---|---|
| 继承 `crypto-ten-*` 前缀的事件 | 7 条（1 条 observation、4 条 data_reject、2 条 data_gap） |
| 继承事件最晚观察截止 | `2026-08-22T11:20:55Z` |
| 修正 `crypto-forty-*` 事件最早观察截止 | `2026-08-22T11:25:55Z` |
| 全部事件的 contract | 一致为 `tradingagent.crypto.forty_symbol_observation_event.v1`（含 7 条继承事件） |

7 条继承事件保留在 append-only 日志中作为历史痕迹，不改写、不删除；其风险
仅限 event_id 命名空间历史重叠，契约字段本身正确。

## 40 币观察器零成功（新开放项）

全日志 reason 分布：`crypto_observation_watermark_invalid` 166 条、
`crypto_observation_query_shape_invalid` 34 条、其余为 gap/无 reason。

判别证据：十币观察器在同一时段以同一数据面、同一 5 分钟节奏持续产出成功
observation（最新 `2026-08-23T04:00:55Z`），故可排除 TradingDatas 数据面
新鲜度回归；失败集中于 40 币运行时自身的 fail-closed 判定（水位校验要求全部
40 个数据集的 `data_through ≤ observed_at ≤ cutoff` 且 freshness 完好，
查询形状校验要求单页行数精确等于 BAR_COUNT）。

下一步（后续批次）：用只读探针逐数据集复现水位/查询形状判定，定位是
per-symbol 页形状参数、40 路请求预算还是元数据时序问题，再做前向修复；
在其恢复产出真实成功观察之前，该通道不能作为任何 receipt-bound 评估的输入。

## 边界

- 未启用、未停用、未修改任何 systemd timer/service；未写任何状态文件。
- 生产 API 访问仅为带 token 的只读 catalog/query 探测，token 未落盘、未输出。
