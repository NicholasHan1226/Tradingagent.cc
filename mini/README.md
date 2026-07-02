# Mac Mini execution bridge contract

> **阅读顺序：** [../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件

本文定义 TradingAgent 服务器与 Mac Mini 执行桥之间的文件合同。服务器端只生产
`signals/pending/*.json` 信号卡；Mac Mini 通过独立 cron 拉取、领取、执行或通知，并把结果写回
`signals/filled/` 与 `signals/positions/`。服务器端不得 SSH 到 Mini，不得直接点击、确认或撤销真实账户委托。

## 数据流

1. TradingAgent 服务器写入 `signals/pending/{order_id}.json`。
2. Mac Mini cron 通过 `rsync`、`git pull` 或 shared volume 拉取 `signals/`。当前合同只约定目录与 JSON 字段，不绑定同步实现。
3. Mini 原子领取一个 pending 信号：`signals/pending/{order_id}.json` rename 到 `signals/claimed/{order_id}.json`。
4. Mini 按 `capital_layer` 分发：
   - `simulated`：校验模拟账户字段后调用本机模拟盘执行器。
   - `real`：只发邮件给 Nicholas 手动确认，并执行只读账户同步。
   - 其他层级不由 Mini 执行桥下单。
5. Mini 写回结果：
   - 成交事实写 `signals/filled/{order_id}.json`。
   - 账户持仓快照写 `signals/positions/{snapshot_id}.json`。
6. 服务器端通过 Hermes bridge 只读读取 `signals/filled/` 和 `signals/positions/`，用于复盘、记账或风控校验。

## 模拟盘合同

模拟盘信号必须同时满足：

- `capital_layer=simulated`
- `account_type=simulated`
- `direct_execution` 可为 `false` 或缺省；Mini 不把它解释为真实账户权限。

通过校验后，当前 live Mini 链路调用：

```bash
~/.hermes/scripts/sim-signal-executor.py
```

服务器侧通过 `Ashare/sim_executor.py` / `shared.execution.webhook_sender` 把 simulated signal 送到 Mini receiver；`mini/mini_consumer.py` 仅保留为历史参考和测试兼容，不是 live 进程。Mini live executor 把执行器返回值归一化为 fill card：

- `order_id`
- `status=filled`
- `filled_price`
- `filled_qty`
- `slippage`
- `fee`
- `executed_at`
- `account_type=simulated`
- `capital_layer=simulated`
- `idempotency_key`

模拟盘执行只允许触达同花顺模拟账户或模拟执行器，不得读取或操作真实账户委托入口。

## 实盘合同

实盘信号必须同时满足：

- `capital_layer=real`
- `account_type=real`
- `direct_execution=false`
- `manual_confirm_required=true`

Mini 对实盘信号只做两件事：

1. 发送邮件 stub 给 Nicholas，内容为信号摘要、风险字段、手工确认提示。
2. 使用只读链路同步账户持仓快照，并写入 `signals/positions/`。

Mini 永远不得对实盘调用模拟执行器、真实同花顺执行脚本、点击交易按钮、提交委托、撤单或确认委托。

若收到 `capital_layer=real` 且 `direct_execution=true` 的信号，Mini 必须拒绝，并把领取后的信号转为 `failed`。这是安全红线，不允许降级执行。

## 账户类型校验

账户字段是 Mini 执行桥的硬门禁：

- `capital_layer=simulated` 必须配 `account_type=simulated`，否则拒绝。
- `capital_layer=real` 必须配 `account_type=real`，否则拒绝。
- `capital_layer=real` 必须配 `direct_execution=false`，否则拒绝。
- `shadow` 或历史 `paper` 只作为影子验证口径，不由 Mini 执行桥真实或模拟下单。

拒绝后的信号应写入 `signals/failed/{order_id}.json`，并附带 `failure_reason`。

## 失败与重试

Mini cron 的失败策略参考 `shared/orchestrator_design.md` 的 Level 1-3：

- Level 1：执行器临时失败、文件同步瞬断、JSON 短暂读写冲突时，在本次 cron 内最多重试 3 次，指数退避 1s/5s/25s。
- Level 2：上游数据未同步、执行器未就绪、只读账户快照暂不可用时，在当前交易阶段结束前重试一次。
- Level 3：跨阶段仍未完成时，写入 `failed` 并保留 `failure_reason`，由下一阶段 repair queue 或人工复核决定是否重新生成信号。

状态迁移约定：

- 可执行领取：`pending -> claimed`
- 执行成功：`claimed -> filled`
- 校验失败：`claimed -> failed`
- 执行器失败且重试耗尽：`claimed -> failed`
- 服务器取消 pending：`pending -> cancelled`
- Mini 已领取后收到取消请求：保留 `claimed` 或后续 `filled`，由状态机记录 `cancel_requested`；成交事实优先。

## pending 过期清理

服务器端负责按 `valid_until` 把过期 pending 信号移动到 `signals/expired/`。Mini cron 在领取前可调用同一状态机 sweep；已领取的 `claimed` 不应由过期清理直接删除，必须通过失败、成交或人工修复闭环。

## 目录与 schema

- `shared/execution/signal_card_schema.json`：pending/claimed/running/filled 状态下的信号卡合同。
- `shared/execution/fill_card_schema.json`：Mini 写回成交事实的字段合同。
- `shared/execution/positions_snapshot_schema.json`：Mini 只读账户持仓快照合同。
- `mini/mini_consumer.py`：Mac Mini 端参考消费者，不是服务器端执行器。

## 安全边界

- TradingAgent 服务器只写 pending 信号卡。
- Mini 模拟盘只调用模拟执行器。
- Mini 实盘只发邮件和只读同步账户。
- `direct_execution=true` 的 real 信号必须拒绝。
- 任何账户类型不匹配必须拒绝。
- 真实账户凭据、验证码、Token、支付密码不得写入信号卡、日志、报告或仓库文件。
