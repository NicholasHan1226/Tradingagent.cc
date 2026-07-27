# A股动态 manifest 服务器只读验收报告

> 日期：2026-07-27 CST
> 范围：TradingAgent 只读消费 TradingDatas 正式内部 API；不修改
> TradingDatas，不启动 front、worker、timer、模拟成交或真实交易。

## 结论

动态 catalog/manifest builder 已进入 TradingAgent 主线并安装为服务器不可变
release。专用 `tradingagent` 身份成功读取正式 `GET /v1/catalog`，确认 190 个
可发现合同中 92 active、98 paused；系统没有把新增 active dataset 自动加入研究。
calendar 与 security-master 当前健康，daily 为 stale/degraded，因此 builder
按设计退出 2，未创建或更新 manifest。该结果证明失败关闭和动态目录适配生效，
不证明新交易会话 observation 或自动模拟盘已经运行。

## 代码与发布层

- PR：[#35](https://github.com/NicholasHan1226/Tradingagent.cc/pull/35)
- 候选提交：`e980d62bab685bf87cbd2f515f07fe2dfd081f32`
- 主线合并提交：`94fcdf767e9e531b18caa1ac0e9ea18cbb1af647`
- GitHub CI：`front` PASS，`test` PASS。
- 本地聚焦候选测试：2434 passed。
- 本地全量回归：3708 passed。
- 服务器不可变 release：
  `/opt/investment/releases/tradingagent/94fcdf767e9e531b18caa1ac0e9ea18cbb1af647`
- 服务器证据：
  `/opt/investment/release-evidence/tradingagent/20260727T085600Z-ta-ashare-manifest-94fcdf7`

release 为 `root:root 0555` 根目录，未切换现役 `current`，未改已存在且含未跟踪
运行资产的服务器工作树。

## 正式数据面读回

固定消费面：

```text
http://127.0.0.1:18082
GET /v1/catalog
POST /v1/query
```

TA 专用 Bearer token 仍只从既有私有 token-file 注入；本轮未读取、打印、复制或
哈希 token。没有访问 SQLite、`/tushare`、`/source_status`、provider 专用 route
或旧 8082。

目录读回：

```text
api_version=v1
catalog_version=v1-ee2dbdf4ecc91390
total=190
active=92
paused=98
active_contract_sha256=cbf7b503b0f003b43048d6d64a49098bbb10db9cc8e82379a26a7b84b0d34b2b
```

核心 catalog metadata：

| Dataset | runtime | degraded | receipt/data-through/observed-at |
|---|---|---:|---|
| `cn.market.trade_calendar` | success | false | present |
| `cn.equity.security_master` | success | false | present |
| `cn.equity.daily` | stale | true | present |

builder 已成功完成认证、动态 catalog 解析、完整交易日历查询和 security-master
预检；daily query 被 Evidence Gate 拒绝：

```json
{
  "blocking": true,
  "reason_code": "core_dataset_evidence_rejected:cn.equity.daily",
  "execution_authority": false,
  "simulation_started": false,
  "real_trading_enabled": false
}
```

退出码为 2，隔离 manifest root 未创建；因此没有旧 current 被覆盖，也没有用
HTTP 200、receipt 存在或 92 active 掩盖 stale/degraded 状态。

## 运行态读回

- `tradingagent-ashare-observation.service`：inactive/static。
- `tradingagent-ashare-observation.timer`：不存在。
- `REAL_TRADING_ENABLED=false`。
- `tradingagent-front-api.service`：inactive。
- `127.0.0.1:8787`：closed。
- TradingDatas `18082`：listening。
- 旧 `8082`：仍由旧系统所有者保留，本轮未访问或修改。

## 下一停止线

1. TradingDatas 将最新完成交易会话的 daily 恢复为
   ready/fresh/valid/degraded=false；
2. 以相同不可变 release 重跑 builder，要求新 manifest 内容寻址发布；
3. 再运行完整 observation runner，验收终端分页、same-observation 双跑、五项
   committed binding、精确幂等和失败恢复；
4. 以上全部通过前不安装或启用 timer，不开始自动模拟交易。
