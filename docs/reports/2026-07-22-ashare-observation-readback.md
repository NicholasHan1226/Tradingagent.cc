# A股 TradingDatas 真实只读观察验收

> 运行日期：2026-07-22 CST。该报告是一次性服务器只读证据，不是 TradingDatas、TradingAgent 现役切换或任何交易权限证明。全程 `REAL_TRADING_ENABLED=false`、`marketgraph_mode=mg_off`，未记录或输出 Bearer token。

## 结论

- TradingAgent 候选 `a7488e9` 在目标服务器通过正式 TradingDatas `127.0.0.1:18082` 的 provider-neutral catalog/query 读取。
- catalog `v1-17fd5855f5a68229`、schema major 2 的五个冻结数据集完成 bounded cursor、跨页身份守恒和 same-observation 双跑。
- 首份 A 股 current-observation 形成 3041 只沪深主板观察标的；该集合是 **observation universe**，不是 Account Tradable Universe、小资金可行池、候选或订单池。创业板、科创板、北交所个股均被排除。
- `index_classify` 与 `sw_daily` 仅进入行业分类/行业指数环境上下文；本次读回没有成分股 denominator 与 coverage authority，不证明完整行业宽度。
- 精确重放复用相同 snapshot、probe 和 observation receipt，返回 `idempotent_replay=true`。这是当时旧三件绑定的真实读回；它没有今日新契约所需的逐股 membership ledger、forward-history readiness 或 paper-planning decision。
- 未产生 capital、order、fill、outbox、reconcile 或 journal 文件。

## 冻结证据

| 对象 | 读回 |
|---|---|
| candidate | `/opt/investment/tradingagent-candidates/a7488e9` |
| archive SHA-256 | `bf7585f356ba539ca81e0cf182a5c1e504497ff6ee4b1c968d2dae86da937b95` |
| catalog | `v1-17fd5855f5a68229` |
| schema major | `2` |
| integration probe receipt | `d4dfd2357d60e34911caf9567ab407a05d410110a01295a936d314d70cb98bfb` |
| semantic integration snapshot | `57f692f99f2ff90470ed4316d6843e3303642466691b14ff82eb29d26bc0dea2` |
| observation snapshot identity | `6c44ab3d02eb3c5a031dc96f049980aaa4d21a5df734d91f83d755d6f0444c1a` |
| probe receipt identity | `0dca80029d3d4d488a40e867be008930ebfe5d662ccebf9f6f8ff9b3418c40cb` |
| observation receipt identity | `7e812ce59fec123fe5aaecde2f5f0a0fd551b5de8641ca5110332dada84c74b9` |
| universe identity | `6f02b5e025e7eede899c5d4d79fe5923ef06e3e9c48d1220e97e16c441a9de83` |
| universe count | `3041` |

五个数据集的真实分页读回：trade calendar 1 行/1 页、security master 5609 行/12 页、daily 5526 行/12 页、index classification 511 行/2 页、SW daily 439 行/1 页。所有数据集均通过逐 dataset freshness/quality/lineage/source receipt gate；HTTP 200 未替代这些检查。

上表 `universe identity/count` 是 2026-07-22 旧 aggregate receipt 的历史真值，不应改写或伪装为新 membership ledger 身份。新 runner 必须使用 fresh state root 同时产生 `ResearchDataSnapshot + probe receipt + observation receipt + observation membership artifact` 四项数据证据，并在精确读回后发布 transaction-complete commit proof；可消费权威是这五项 committed binding。旧 `a7488e9` state 不得补写或直接当作新契约的 replay 起点。

## 主板排除读回

排除个股共 2485 个：北京市场 328、创业板 1398、科创板 609、风险警示 147、新股不足 30 日 2、其它非范围 1。排除原因是观察范围合同，不是对股票质量或收益的判断。

## 文件与身份安全

- 快照、probe、observation receipt 和 snapshot-store decision binding 均为 regular file、single link、`0600`，一次性运行身份为 `marketgraph:marketgraph`。这里的 decision binding 只是快照的不可变索引，不是旧 one-shot 的第四项 committed authority；该列表也不包含当时候选尚未实现的 membership ledger。
- `marketgraph` 只用于这次兼容性只读验收，因为服务器现有只读 token 属于该身份；该身份/token 不得被长期复用。未来 observation worker 只接受独立 handoff 的 `tradingagent:tradingagent` 身份与 TA-scoped token，并保持 `mg_off`；它不会把 MarketGraph 变成 TA 内部模块或关键路径。
- candidate 中关键 runner/unit 文件的 SHA-256 与当时提交 `a7488e9` 的本地冻结字节完全相同；当前工作树后续增加五项 committed binding 后不得继承这条服务器字节证据。服务器 `systemd-analyze verify` 未报告当时该候选 unit 的语法错误，输出中的 warning 来自既有 `cloudmonitor.service`。

## 明确停止线

- 当前 snapshot 是 `current_observation`，`historical_pit_eligible=false`，不能用于历史训练或宣称回测有效。
- 本次旧 one-shot 没有逐股 membership ledger、21 个 forward-collected session、交易日连续性 authority 或公司行动/复权 authority，不证明 history/feature readiness，也不能反向补标。
- 当前 active daily 数据不能提供 bid/ask、盘口数量、30 秒行情 freshness 或分钟成交量，因此 `execution_authority=false`；不得从日线合成成交。
- T 日盘后 daily 只能在独立冻结交易日历授权后映射为 T+1；当前 daily-only planner 只能 `paper_trade_session=null` 且 `action=abstain/status=completed_with_blocks`，不产生 capital/order/fill/outbox/reconcile/SampleJournal。
- 专用 `tradingagent:tradingagent` 身份和 TA scope token 尚未由发布侧安装，observation service/timer 未激活。当前 timer 还缺可信的每日 immutable manifest rollover，是 non-enableable code candidate，不得用静态 `as_of` 重复回放。
- 现役 TradingAgent 前端、旧 service、cron、8787、公开入口和真实交易权限均未改变。
