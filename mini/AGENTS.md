# TradingAgent / Mini

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

本目录是可选 Mac Mini/Hermes 模拟 GUI 对照的参考合同，不是资本、策略、server-local execution 或实盘 authority。

## 当前边界

- A股默认在服务器本地完成 simulated 闭环；只有 `ASHARE_SIM_HERMES_ENABLED=1` 才可把同一 simulated signal 送到 Mini 对照。
- Mini 只领取、校验、模拟执行/确认和回写；不判断买卖、不分配资金、不修改任何 market capital ledger。
- 只接受 `capital_layer=simulated`、`account_type=simulated`、`real_trading_enabled=false`。real/live/direct execution 或真实账户标记必须拒绝。
- 点击不等于成交。只有 actual 委托/成交/持仓证据和完整 fingerprints 才可确认；无法确认时 failed + halt，禁止自动重试下单。
- server-local authority 不依赖 Mini；Mini 故障不得伪造成交或中断 observation。
- 拟议“TA 信号 → 邮件 → Nicholas → 同花顺人工实盘”仍是外部设计，未获审阅、未实现。Mini 当前不得发邮件、读取真实账户、点击真实交易或写 live signal。
- 服务器不得 SSH 到 Mini 直接操作交易 GUI。

文件与状态合同见 [README.md](README.md)，故障与回滚见 [../docs/operations.md](../docs/operations.md)。
