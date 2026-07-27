# TradingDatas 99-active 增量只读报告

> 日期：2026-07-27 CST

## 结论

TradingAgent 以既有专用身份重新读取正式 `GET /v1/catalog`，确认
`catalog_version=v1-71c20445233c890e`、190 total、99 active、91 paused。新增
七个 dataset 只进入动态 catalog inventory；TA 没有为它们新增路由、字段映射、
研究逻辑或执行资格，也没有发送非核心业务查询。核心 daily 仍为
stale/degraded，现有动态 builder 再次退出 2，未发布 manifest。

## 目录事实

```text
active_contract_sha256=876e3d514ec5119b64eabd1ff7ee7e20fe1d7163937dbcb594d43a33969b95e0
non_core_queries_sent=0
research_auto_promotion=false
simulation_started=false
real_trading_enabled=false
```

本轮新增目录项：

| Dataset | Catalog runtime | 业务处理 |
|---|---|---|
| `cn.dataset.broker_recommend` | success | inventory only |
| `cn.dataset.cn_cpi` | empty | inventory only |
| `cn.dataset.cn_gdp` | empty | inventory only |
| `cn.dataset.cn_m` | empty | inventory only |
| `cn.dataset.cn_pmi` | empty | inventory only |
| `cn.dataset.cn_ppi` | empty | inventory only |
| `cn.dataset.sf_month` | empty | inventory only |

必须区分两层状态：catalog runtime 是采集摘要；TradingDatas 发布方提供的真实
`POST /v1/query` envelope 对这七项仍为 `state=partial/degraded=true`，并带
freshness/completeness 未验证原因。TA 不把 catalog 中的 success/empty 当成
ready，也不会因为 receipt 存在而升级研究或执行资格。

## 核心门禁复核

```text
cn.market.trade_calendar  runtime=success degraded=false
cn.equity.security_master runtime=success degraded=false
cn.equity.daily            runtime=stale   degraded=true
```

builder 返回：

```text
exit_code=2
reason_code=core_dataset_evidence_rejected:cn.equity.daily
manifest_root_created=false
```

## 证据与运行边界

- 代码 release：
  `/opt/investment/releases/tradingagent/94fcdf767e9e531b18caa1ac0e9ea18cbb1af647`
- 本轮证据：
  `/opt/investment/release-evidence/tradingagent/20260727T092955Z-ta-catalog99-94fcdf7`
- worker：inactive/static。
- timer：不存在。
- `REAL_TRADING_ENABLED=false`。
- 未读取或修改 TradingDatas 仓、SQLite、token、8082、front 或生产调度。

下一步仍是等待最近完成交易日的 daily 恢复
ready/fresh/valid/degraded=false，再重跑动态 manifest 和完整 observation。

## 99-active API parity 补充

同一 production release 和 catalog 随后完成一次 fresh consumer parity：

```text
identity=UID987 with existing TA read scope
request=POST /v1/query
limit=1
as_of=omitted
active_dataset_count=99
http_200=99
query_contract_failure=0
nonempty=79
legal_empty=20
metadata_ready=3
metadata_partial=92
metadata_stale=4
runtime_success=75
runtime_empty=20
runtime_stale=4
```

这证明所有 active dataset 都能通过固定 catalog/query 合同完成首屏认证回读，
并能把合法空结果与非空结果投影为诚实 metadata。它不证明 99 项完整分页、
response completeness、freshness watermark、历史版本或交易用途。尤其不能以
99/99 HTTP 200 覆盖 92 partial 和 4 stale，也不改变当前 daily 对核心
observation 的失败关闭。
