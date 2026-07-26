# A股当前会话正式只读运行报告

> 日期：2026-07-26 CST
> 范围：TradingAgent 只读消费 TradingDatas 正式内部 API；不修改 TradingDatas，
> 不启用 timer/front/worker，不产生模拟成交或真实交易。

## 结论

当前 A股 observation 代码已经支持“决策自然日”和“最近完整交易会话”分离。
首次运行先被交易日历连续性门禁正确拒绝；TradingDatas 补齐日历后，核心三数据集
重跑形成 `20260724` current-observation，并完成同 state root 精确幂等重放。
行业分类 optional context 在重跑时为 failed/degraded，未进入 snapshot。
没有 feature、ranking、模拟成交或真实交易。

## 代码与发布层

- 本地、`origin/main` 与 GitHub 主线合并提交：
  `6db813cdb9c9eaa36ab65c3529ebaeee145aeba2`
- 变更 PR：
  [#30](https://github.com/NicholasHan1226/Tradingagent.cc/pull/30)
- GitHub CI：`front` PASS，`test` PASS。
- 服务器不可变 release：
  `/opt/investment/releases/tradingagent/6db813cdb9c9eaa36ab65c3529ebaeee145aeba2`
- 服务器证据：
  `/opt/investment/release-evidence/tradingagent/20260726T105403Z-ta-current-session-6db813c`
- release 只安装、未切 current；front、worker、timer 和真实交易均未启动。

## 数据面事实

正式数据面固定为：

```text
http://127.0.0.1:18082
GET /v1/catalog
POST /v1/query
catalog_version=v1-c19a22c011fc363e
```

没有访问 SQLite、`/tushare`、`/source_status` 或 provider 专用路由，也没有
回退到 8082。

最终通过的核心 manifest：

```text
cn.market.trade_calendar
cn.equity.security_master
cn.equity.daily
```

- `cn.equity.daily`：`trade_date=20260724`，上游已采集 5526 行；正式查询
  metadata 为 ready/success、degraded=false、fresh/valid，receipt/lineage 完整。
- `cn.equity.security_master`：显式限制 `list_status=L`，用于当前上市主板范围。
- `cn.dataset.index_classify`：第一次四数据集重跑时返回
  failed/degraded。它是 optional context，因此从核心通过 manifest 排除；
  `context_probe_roles=[]`，当前 observation 不含行业上下文。
- `cn.dataset.sw_daily`：同日上游 QuickSync 返回 `40101` permission-denied，
  未纳入必需数据集并继续 fail-closed。
- `cn.market.trade_calendar`：首次完整分页只证明最近开市日为 `20260722`；
  TradingDatas 随后补齐 `20260723`–`20260725`，最终运行直接读到
  `20260724 is_open=1`。

## 门禁演进与最终通过

首次决策时间为 `2026-07-26T18:55:45+08:00`，目标 daily session 为
`20260724`。TA 要求完整交易日历直接证明该 session 是 decision date 当日或之前
的最近开市日。当时日历只证明到 `20260722`，运行返回：

```json
{
  "blocking": true,
  "real_trading_enabled": false,
  "reason_code": "daily_bars_not_latest_completed_session",
  "status": "fail"
}
```

TA 没有使用 `pretrade_date`、周末推断、自然日或人工判断补造交易会话。失败 state
root 只保留私有目录，没有 committed observation 文件。

日历补齐后，包含 optional `index_classify` 的重跑在 integration probe 阶段继续
失败关闭，因为该数据集的 metadata 已变为 failed/degraded。TA 没有把 optional
context 的旧 ready 状态或历史 receipt 当作当前证据。

最终以新鲜 decision time `2026-07-26T19:08:00+08:00`、新 manifest 和独立
state root 运行三个 required dataset，结果：

```text
status=pass
observation_session=20260724
observation_universe_count=3041
excluded_individual_count=2569
probe_same_as_of_match=true
historical_pit_eligible=false
execution_authority=false
real_trading_enabled=false
```

排除原因：

| 原因 | 数量 |
|---|---:|
| 创业板个股无权限 | 1398 |
| 科创板个股无权限 | 611 |
| 北交所个股不在首阶段 | 330 |
| 风险警示股票 | 147 |
| B股 | 78 |
| 新股最小上市期未满足 | 2 |
| 非首阶段 instrument | 2 |
| 当日日线缺失/不可用 | 1 |

服务器私有 state root：

```text
/var/lib/tradingagent/ashare-observation/current-observations/20260724-6db813c-v3
```

目录为 `tradingagent:tradingagent 0700`，11 个状态文件均为
`tradingagent:tradingagent 0600`。五项 committed evidence：

```text
snapshot_sha256=7e6b02815806011cebaa995d669eea577c305eada3a9eaaecea19b7f760114c5
probe_receipt_sha256=f11c298d172f7d5754821da3f132411949b183ca7986d578e6d0f8e761dec81a
observation_receipt_sha256=8815f8524eacf5d1fae2755df1fcb1f4aa6245ce5291d473a4bc24043538fdd5
observation_ledger_sha256=47f340a4a6ed7a83693dacf1b74a982de87dada771331a1828ee078aa93d56f0
observation_transaction_complete_sha256=4d0c24062235c0f28e5e57f6ffcc9f8547d63ab2602d1fc8c7db2979d1cfe0e8
```

第二次运行返回相同 identities，`idempotent_replay=true`，没有生成第二套权威
状态。

## 验证结果

- 本地聚焦测试：198 passed。
- 本地全量回归：3697 passed。
- 服务器 runtime 测试：156 passed（使用既有 root test environment，仅证明代码
  可运行，不等于专用服务身份 runtime）。
- 不可变 archive 中缺少 `.git`，因此依赖 `git check-ignore` 的单个架构测试不能
  在 archive 内运行；该失败已原样保留，不被包装成 PASS。
- 服务器 `/opt/tradingagent/venv` 的 parent `/opt/tradingagent` 为
  `root:marketgraph 0750`，UID 987 无法进入；专用身份运行环境仍需单独修复。

## 运行态与残余旧链

- TradingDatas 18082：active。
- TradingDatas collector timer：not-found/inactive，本轮未修改。
- TradingAgent front：inactive，但 unit file 仍 enabled。
- 8787：closed。
- 旧 8082：仍 listening，本轮未修改。
- inactive front 的遗留 drop-in 仍指向
  `SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`。当前 observation consumer
  不使用该配置，但在旧 front 退役或恢复前必须先清零依赖并独立验证。
- `REAL_TRADING_ENABLED=false`。

## 下一停止线

1. 在任何 scheduler 激活前，先提供 UID 987 可执行的不可变 Python runtime，
   并清理 inactive front 的 8082 遗留配置；
2. worker 必须按当前 catalog、最近完整交易日和当前 decision time 生成新鲜
   manifest/state root，不能重放静态 `20260724`；
3. 先以 disabled unit 做下一交易日 dry-run、幂等和失败恢复验证，再决定是否启用
   observation timer；
4. 逐日积累至少 21 个 forward session；行业上下文恢复健康后再独立加入；
5. 分钟行情、模拟成交政策、资本账本和对账门禁未通过前，不生成 PaperFill。
