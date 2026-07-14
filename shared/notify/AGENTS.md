# notify/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

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
9. capital_plan.py (含盘前) — 当前50k模拟盘资金规划+逆回购
10. strategy_invalidation.py (regime变化) — 变化+影响+调整
11. system_health.py (异常时) — 采集+管线+完整性

## 原则
- 固定模版, 图表>文字, 总结性语言
- 减少系统术语, 不出agent名字
- 紧急告警10分钟自愈期, 不行人工
- delivery、fallback 和审计状态不得改写上游业务 PASS/FAIL；调用方必须分别保留业务结论与通知结果。
- 本地 fallback 遇到 `PermissionError` 或其它 `OSError` 时，必须返回结构化 `degraded` 结果并带明确的审计错误；不得把异常静默为已送达。
- 业务 FAIL 仍由调用方以原有业务语义和退出码保持失败；通知降级不得伪造业务成功或通知已送达。

## 发送、路由与降级边界

- Cloudflare Email Service 发送只使用 `/email/sending/send`；`/email/routing/messages` 不是发送入口，禁止作为 fallback。
- 交易与系统通知分别使用本文件通道表中的默认发件人与收件人；健康、告警、自愈和对账属于系统通道，其余交易信号、规划、复盘、报告和回执属于交易通道。
- 调用方显式指定 `channel="system"` 时，优先使用系统通道默认收件人，除非同时显式传入 `to`。
- 发送失败可保存到 `shared/notify/logs/email_fallback/` 作为审计 fallback；只有 `status=sent` 能证明已送达，fallback 文件存在不构成发送成功。
