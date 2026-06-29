# Mac Mini signal-card executor

Mac Mini cron每5min: 拉取signals/pending/ -> 调用a_share_simulated_trade_executor.py -> 写回signals/filled/ + positions.json

## 边界

- 服务器端 Tradings 只生成 JSON signal card, 不 SSH 到 Mac Mini。
- Mac Mini 独立 cron 消费 `/opt/investment/Tradings/signals/pending/`。
- 执行结果写入 `/opt/investment/Tradings/signals/filled/{order_id}.json`。
- 最新持仓快照写入 `/opt/investment/Tradings/signals/positions.json`。
- 取消请求由服务器把 pending card 移到 `/opt/investment/Tradings/signals/cancelled/`。
- 真实资金自动下单永远禁止；该桥只负责文件通信和人工/模拟执行前置卡片。
