# notify/

## 目标
11类邮件模版 + 双通道告警路由。

## 通道
| 类型 | 发件 | 收件 | 用途 |
|------|------|------|------|
| 交易类 | notice@tradingagent.cc | tradingadviser@coze.email | 信号/规划/复盘/日报/回执 |
| 系统类 | notice@tradingagent.cc | soc@coze.email | 健康/告警/自愈/对账 |

## 模版 (11类)
1. pre_market_plan.py (8:30) — 持仓+资金+行情+板块+策略
2. trading_signal.py (触发时) — 股票+条件+评分+操作+仓位
3. midday_review.py (11:35) — 上午表现+下午计划
4. closing_plan.py (14:30) — 持仓+尾盘操作+隔夜风险
5. daily_report.py (15:30) — 交易汇总+盈亏+归因+明日
6. weekly_report.py (周五) — 策略统计+趋势+下周
7. trade_receipt.py (成交后) — 确认
8. emergency_alert.py (异常, 10min自愈) — 类型+影响+动作
9. capital_plan.py (含盘前) — 20k分配+逆回购
10. strategy_invalidation.py (regime变化) — 变化+影响+调整
11. system_health.py (异常时) — 采集+管线+完整性

## 原则
- 固定模版, 图表>文字, 总结性语言
- 减少系统术语, 不出agent名字
- 紧急告警10分钟自愈期, 不行人工

## 2026-07-01 邮件链路修复
- Cloudflare 发送接口使用 Email Service endpoint `/email/sending/send`，旧的 `/email/routing/messages` 会导致 404/鉴权失败。
- 交易邮件固定：`notice@tradingagent.cc -> tradingadviser@coze.email`；系统邮件固定：`notice@tradingagent.cc -> soc@coze.email`。
- 发送失败时仍保存到 `shared/notify/logs/email_fallback/`，但修复后必须用真实模板邮件验证 `status=sent`，不能只看 fallback 文件存在。
