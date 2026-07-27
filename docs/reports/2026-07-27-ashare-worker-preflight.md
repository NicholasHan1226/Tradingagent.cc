# A股 worker 发布前预检

> 日期：2026-07-27 CST。本文只记录本轮 TA 服务器事实，不授权启动 worker、
> timer、模拟成交或真实交易。

## 结论

当前权威 `main=724ea8818feff142df57c4a7bf7b558e29ec0a35` 已作为不可变、
root-owned、只读 release 安装到服务器，但没有创建 `current` 指针、没有修改
systemd、没有安装环境文件，也没有启动任何任务。动态 manifest builder 继续对
`cn.equity.daily` 的 stale/degraded 元数据 fail closed。

TradingDatas 已确认 `trade_date=20260724` 的日线采集实际成功，5526 行和成功
receipt 均存在；当前阻塞来自通用 freshness 投影在周末错误比较交易日分区零点与
墙上时钟。因为 API 仍返回 `state=stale`、`runtime_state=stale`、
`degraded=true`、`freshness_sla_exceeded`，TA 不得发布 manifest 或启动模拟闭环。

## 本轮服务器读回

- 不可变 release：
  `/opt/investment/releases/tradingagent/724ea8818feff142df57c4a7bf7b558e29ec0a35`
- release staging 证据：
  `/opt/investment/release-evidence/tradingagent/20260727T121840Z-ta-release-stage-724ea88`
- builder 重跑证据：
  `/opt/investment/release-evidence/tradingagent/20260727T121910Z-ta-manifest-preflight-724ea88`
- worker 安装预检：
  `/opt/investment/release-evidence/tradingagent/20260727T122011Z-ta-worker-install-preflight-724ea88`
- builder 退出码：`2`
- builder reason：
  `core_dataset_evidence_rejected:cn.equity.daily`
- 隔离 manifest root：为空，没有 current manifest 或内容寻址 manifest 被发布。
- `REAL_TRADING_ENABLED=false`
- worker：`inactive/static`
- timer：`inactive/not-found`
- 8787：未监听
- TradingDatas：正式 18082 继续监听；TA 只使用 catalog/query。

## 尚未安装的运行差异

当前 `/etc/systemd/system/tradingagent-ashare-observation.service` 是旧版 unit：
它没有动态 manifest builder 的 `ExecStartPre`。此外：

- `/opt/investment/releases/tradingagent/current` 不存在；
- `/etc/tradingagent/ashare-worker.env` 不存在；
- 新 unit 的 `systemd-analyze verify` 已通过；
- installed unit 与 tracked unit 字节不同，差异已冻结在预检证据目录；
- 本轮没有修改 `/etc/systemd/system`，因为正常 standing authorization 明确不
  自动覆盖 service 安装或启用。

## 后续停止线

1. TradingDatas 先回传交易会话感知 freshness 修正后的 immutable handoff；
2. TA 重新执行 builder，只有三个核心 dataset 均通过 Evidence Gate 才发布
   manifest；
3. 另行确认后才能安装 tracked unit、secret-free env 和 `current` 指针；
4. unit 保持 inactive/static，先手工 one-shot 验证幂等、失败恢复和状态写入；
5. timer 与自动模拟成交继续留在更后的独立门禁。

回滚只允许恢复 byte-exact unit/env/current 指针并保持 worker 停止；append-only
observation、receipt 和 evidence 不得删除或改写。
