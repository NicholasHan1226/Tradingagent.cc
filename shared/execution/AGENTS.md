# execution/

## 目标
订单执行层: Hermes(A股实盘) + 影子盘(多策略并行) + 模拟盘(滑点建模)。

## 自动化级别
5-10分钟级自动化: 信号触发→路由→模拟/影子记录→(实盘待人工确认)。

## 文件
- hermes_bridge.py — A股信号卡桥, 只写入/读取 signals JSON 文件, 不 SSH 到 Mac Mini, 不直接执行
- shadow_broker.py — 影子盘, 仅记录不执行, 多策略并行
- sim_broker.py — 模拟执行, 含滑点建模
- execution_router.py — 按策略阶段路由 (sim→shadow→real)
- slippage_model.py — 滑点模型 (市价/限价)

## 边界
- 真实资金只给Nicholas手工确认, 不自动下单/撤单/点击
- 影子盘和模拟盘可全自动记录
- Hermes桥只生成信号卡, Mac Mini cron 独立拉取 pending 信号并写回 filled/positions
