# Mac Mini simulated execution bridge

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

Mini/Hermes 只是可选的 A股 simulated GUI 对照路径。权威执行是服务器本地 fresh lineage root `shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/`，权威资本仍是 A股独立 market capital ledger。Mini 不承担策略、资金或实盘职责。

## 启用条件

默认关闭：

```bash
export ASHARE_SIM_HERMES_ENABLED=0
export ASHARE_SIM_WEBHOOK_ENABLED=0
```

只有单独验证 Mini 健康、文件合同和 simulated 账户隔离后，才可将 `ASHARE_SIM_HERMES_ENABLED=1` 用于模拟对照。启用不改变 server-local authority，也不赋予真实交易权限。

## 接受的信号

Mini 只接受同时满足以下条件的信号：

```json
{
  "capital_layer": "simulated",
  "account_type": "simulated",
  "real_trading_enabled": false,
  "direct_execution": false
}
```

还必须包含稳定 order/idempotency ID、market/symbol/side/quantity、PIT timestamp、candidate/execution source、capital authority/generation/execution lineage 和 payload fingerprint。

任何 real/live、真实账户、broker credential、`direct_execution=true` 或缺 simulation-only flags 的信号都写入 failed，不能降级执行。

## 状态流

1. 服务器将可选 simulated signal 写入 `signals/pending/{order_id}.json`。
2. Mini 原子 rename 到 `signals/claimed/{order_id}.json`。
3. Mini 校验 schema、fingerprint、幂等和 simulated account。
4. 模拟执行后，只在有 actual 委托/成交/持仓证据时写 `signals/filled/{order_id}.json` 与 `signals/positions/{snapshot_id}.json`。
5. 无法确认、证据冲突或账户不匹配时写 `signals/failed/{order_id}.json` 并 halt；不得自动生成替代订单。

状态迁移：`pending → claimed → filled|failed`。过期 pending 可移动到 `expired`；已 claimed 记录不能删除或覆盖。取消请求与实际成交冲突时保留全部事件，成交事实优先。

## Fill 回写

确认的 simulated fill 至少包含：

- order/idempotency/fill ID 与 fill sequence；
- `status=filled|partial`、actual price、actual quantity、timezone-aware executed time；
- actual fee/slippage；
- account/capital simulation flags；
- candidate/prediction snapshot、execution source 和 market evidence；
- receipt/position/payload fingerprints；
- capital authority/generation/execution lineage。

Mini 回写不是 capital commit。服务器仍需用 durable outbox 把 immutable fill 提交到 A股 market capital ledger；commit pending/failed 不能进入 execution-eligible 策略绩效。

## 失败语义

- 网络、同步或 Mini health 异常：服务器继续 local simulated observation/执行，禁用 webhook；不伪造 Mini fill。
- Mini busy/halted：不发送新 webhook；保留现有 queued facts。
- actual evidence 不完整：failed + halt，不自动重试下单；可保留 chain-validation 样本。
- payload/fingerprint/idempotency 冲突：安全失败并人工复核。
- real/live marker：安全事件，拒绝且不得写入当前 SampleJournal。

## 文件 schema

- `shared/execution/signal_card_schema.json`
- `shared/execution/fill_card_schema.json`
- `shared/execution/positions_snapshot_schema.json`
- `mini/mini_consumer.py`：参考消费者，不是 server-local authority。

## 未实现的未来路径

“TA 信号 → 邮件 → Nicholas 在同花顺人工复核下单”在当前线程外只读设计中。本目录不发送邮件、不生成 real signal、不读取真实账户、不点击真实交易，也不提供 broker automation gateway。

即使未来设计获批准，也必须与 simulated bridge 物理/逻辑隔离，并经过独立授权、人工确认、风控、审计和回滚验收；不能复用当前 Mini simulated 权限自动切换。
