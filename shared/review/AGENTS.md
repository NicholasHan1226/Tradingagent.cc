# tradingagent/shared/review

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 目标
复盘系统: 3组对比(实际vs预期目标 / 实际vs基准沪深300 / 实际vs上一周期) + 归因 + 行动。
自愈闭环: 巡逻→修复→记忆→复盘→迭代。向稳定胜率与收益进化。

## 文件结构
- daily_review.py — 每日2次复盘(午盘11:35 + 收盘15:30)
- weekly_review.py — 周五复盘(策略胜率/维度有效性/升降级)
- sim_ledger_reader.py — 只读加载 unified simulated ledger 与 A股 server-local simulated ledger, 作为复盘/报告输入
- monthly_review.py — 月末复盘(架构健康/记忆固化/目标达成)
- attribution.py — 收益归因到维度/策略/条件
- benchmark.py — 基准追踪(沪深300/创业板/买入持有/上一周期)
- goals.yaml — 阶段目标(stage_1_sim → stage_4_scaled)
- opportunity_funnel.py — 机会漏斗事件写入规范; 只写 `shared/review/opportunities/funnel_events.jsonl`, 供前端动态漏斗播放, 不改变信号/订单状态
- self_heal_loop.py — 闭环自愈系统(巡逻→修复→记忆→复盘→迭代)

## 三组对比框架
1. 实际 vs 预期目标 — 对照 goals.yaml 当前阶段门槛,判定是否达标
2. 实际 vs 基准(沪深300) — alpha/beta/sharpe/最大回撤,是否跑赢
3. 实际 vs 上一周期 — 同比改善/退化,趋势方向

## 归因
将收益拆解到:
- 维度: 宏观/事件/基本面/资金/技术/情绪
- 策略: 回调/趋势/突破/事件驱动/...
- 条件: 单仓/行业/时段/波动率 regime

## 复盘输入
- `load_shadow_trades()` 保持旧兼容: 仅读旧 shadow trade log / filled signals fallback。
- `load_review_trades()` 是日报、周报、归因和邮件报告的默认入口: 合并 legacy shadow fills、`shared/logs/sim_ledger/<market>/<style>/trade_journal.jsonl` 和 A股 `shared/logs/local_sim/local_sim_trades.jsonl`。
- A股 server-local simulated 账本是账户事实; 非连续竞价时段成交必须保留在账户/持仓/回执里, 但由 `sample_quality.py` 标为 `outside_ashare_regular_session` 链路验证样本, 不得进入策略 PnL、胜率、方向命中、周度升降级或自我演化。
- HK 暂不进入默认生产复盘输入; 如未来恢复 HK, 必须显式把 HK 放回生产市场范围并同步健康检查。
- 报告必须同时保留 `review_trade_count`、`shadow_trade_count`、`simulated_trade_count`，避免把模拟盘样本误判为 0。

## 自愈闭环
巡逻(patrol)检出 → 修复(heal)处置 → 写入记忆(memory) → 复盘(review)迭代规则。
修复失败 → 10分钟紧急告警 → 升级人工。

## 升降级纪律
- 降级: 胜率<50% 连续2周 → 策略降权/下线
- 升级: 当前资金层连续2周为正 → 升级到更高权重或下一验证层; 当前生产主验证层为 simulated, 实盘仍需人工安全门
- 冻结模型期: 不允许 in-sample 调参(纪律红线)

## 原则
- 复盘是行动的依据, 不是仪式
- 每次复盘必须产出 next_day_plan / next_week_plan / next_month_focus
- 记忆是闭环的脊柱: 无记忆则无迭代
