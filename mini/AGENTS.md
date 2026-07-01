# Mini — Mac Mini 执行桥（参考副本）

> **阅读顺序：** [../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件

> **⚠️ 本目录是本地参考副本。** 真实 Hermes 执行桥运行在 Mac Mini `~/.hermes/` 下。
> 本目录的 `mini_consumer.py` 是参考消费者代码，不是服务器端执行器。

## 本目录职责

- 定义服务器与 Mac Mini 之间的文件合同（信号卡 schema、回执格式、状态迁移）
- 提供参考消费者实现 `mini_consumer.py`
- 说明信号卡从 `pending → claimed → filled/failed` 的状态机

## 真实 live 路径

| 组件 | 路径 |
|------|------|
| Hermes 执行器 | Mac Mini `~/.hermes/scripts/sim-signal-executor.py` |
| 信号接收器 | Mac Mini `~/.hermes/scripts/sim-signal-receiver.py` |
| 运行时状态 | Mac Mini `~/.hermes/ashare-runtime/signals/` |
| launchd 服务 | `com.nicholashan.sim-signal-receiver` / `com.nicholashan.sim-signal-executor` |

## 安全红线

- 服务器不得 SSH 到 Mini 直接操作
- 实盘信号 (`capital_layer=real` + `direct_execution=true`) 必须拒绝
- 真实账户凭据不得出现在信号卡、日志或仓库文件中
- 模拟盘只调用模拟执行器，不得触达真实账户

## 相关文档

- [AGENTS.md](../AGENTS.md) — TradingAgent 总规则
- [mini/README.md](README.md) — 执行桥合同详情
- [docs/runtime_incidents_20260701.md](../docs/runtime_incidents_20260701.md) — 7/1 执行事故链复盘
