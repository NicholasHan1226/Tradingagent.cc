# TradingAgent

交易执行全闭环。从筛选到执行到复盘，以稳定胜率和回报率为方向持续进化。

> **阅读顺序：** 进入 TradingAgent 后，先读 [AGENTS.md](AGENTS.md) → [STATUS.md](STATUS.md) 了解规则和当前状态。本文件提供系统概述和架构总览。

## 目标
构建自动化交易系统：主动发现机会 → 条件驱动捕捉 → 风控保护 → 复盘进化。

## 核心理念
1. 权重式打分，不设硬门禁 — 某维度弱不排除，只拉低综合分
2. 条件驱动，主动发现 — 不等信号触发才发现，预设计条件自动捕捉
3. 基本面预计算 — 全市场预评分，主动发现低估/成长股
4. 持续优化 — 复盘调整权重/参数/策略，以胜率和回报率为方向进化

## 架构
```
shared/ (跨市场共享):
  screening/    六维打分(宏观/事件/基本面/资金/技术/情绪) + 条件订单
  adversarial/  多空对辩 + 压力测试 + 历史类比
  risk/         事前风控 + 持仓监控 + 黑天鹅应急
  portfolio/    组合构建 + 仓位分配 + 再平衡 + 退出
  execution/    Hermes同花顺 + 影子盘 + 模拟盘 + 滑点建模
  notify/       11类邮件模版 + 告警路由(10min自愈)
  review/       日复盘(2次) + 周复盘 + 归因 + 基准 + 自愈闭环
  accounting/   资金记账 + 持仓记账 + 对账 + 审计
  benchmark/    沪深300/创业板/买入持有基准

Ashare/   A股(T+1, Hermes同花顺, 集合竞价+连续+收盘竞价)
Crypto/   加密(24/7, API可执行)
US/       美股(Alpaca API)
HK/       港股(预留)
PM/       预测市场(CLOB sandbox)
```

## 交易流程 (条件驱动漏斗)
```
全市场预计算(日级) → 六维打分排序 → 候选池分层(5层)
→ 对抗分析(多空对辩+压力测试) → 信念分
→ 风控筛选(降权不硬拒) → 组合构建(风险平价)
→ 条件订单生成 → 5min实时监控 → 条件触发
→ 执行(模拟/影子/实盘) → 持仓状态机
→ 退出(止损/止盈/时间/逻辑证伪) → 复盘
→ 调整权重/参数 → 下一轮进化
```

## 候选池分层 (不同层不同频率)
```
A: 持仓池      — 实时/5min监控退出+调整条件
B: 一级观察    — 5min监控买入触发条件
C: 二级候选    — 15min/小时监控维度变化
D: 全市场      — 日级打分升降级
E: 基本面深度  — 周/季度财报更新
```

## 六维打分 (权重式, 不设门禁)
```
宏观面(0.15) ← MarketGraph regime
事件面(0.20) ← SharedSignals raw_events
基本面(0.25) ← Tushare财务预计算
资金面(0.15) ← moneyflow/北向/融资融券
技术面(0.15) ← 行情计算(动量/弹性/突破)
情绪面(0.10) ← 换手/涨跌比/温度
→ 综合分排序, 取Top N, 不排除任何股
```

## 邮件通道
- 交易类: notice@tradingagent.cc → tradingadviser@coze.email (11类模版)
- 系统类: notice@tradingagent.cc → soc@coze.email

## 资金
- 初始20,000元, 集中2-3只, 闲置买逆回购(204001)
- 盘前1小时资金规划邮件
- 小资金集中, 影子盘测分散策略

## 执行
- 模拟盘: UI自动化 (新策略验证期)
- 影子盘: 多策略并行记录 (已验证策略平行运行)
- 实盘: Hermes桌面控制同花顺 (5-10min级别, Mac Mini)
- US: Alpaca API (未来实盘)
- 升级路径: 模拟→影子→小实盘→规模化

## 复盘 (3对比+归因+行动)
```
对比1: 实际 vs 预期目标 (胜率55%+/夏普0.5+/回撤<10%)
对比2: 实际 vs 基准 (沪深300, 跑赢多少)
对比3: 本期 vs 上期 (趋势改善/恶化)
归因: 哪个维度/策略/条件贡献收益或损失
行动: 继续/停止/调整什么
```

## 与其他层的关系
- ← SharedSignals: 只读行情+事件+基本面+资金
- ← MarketGraph: 只读regime+event_impact+forward_calendar+scenario
- → MarketGraph: 价格结果反馈(纯价格, 非交易, 用于因果验证)
- → 不回传: 交易决策不回传(保持研究独立)

## 边界
- 做: 选股/择时/风控/执行/复盘
- 不做: 不采集数据（SharedSignals负责）
- 不做: 不做宏观研究（MarketGraph负责）
- 不做: 不修改因果规则（只消费MarketGraph输出）

## 仓库
https://github.com/NicholasHan1226/Tradingagent.cc.git

## 现有工具
- Ashare: 144个工具 (因子28/复盘27/组合18/执行16/筛选11/风控10/通知14)
- Crypto: 21个 / US: 20个 / PM: 20个 / HK: 预留
- shared/: 50个.py文件 (筛选/对抗/风控/组合/执行/通知/复盘/记账)

## 运维报告

TradingAgent 每小时生成一次统一运维报告：

```bash
PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/ops_report.py --send-on never --pretty
```

报告文件：

- `shared/review/ops/tradings_ops_latest.json`
- `shared/review/ops/tradings_ops_history.jsonl`

覆盖范围：执行队列、影子队列、失败原因聚合、Mini/Hermes 回执完整性、服务器本地模拟账本和影子盘 PnL 摘要。系统邮件只在 `overall_status=fail` 时发送到 `soc@coze.email`；`warn` 仅记录在报告里。

### 回执指纹口径

- `payload_sha256`: Mini receiver 收到的原始任务包指纹。
- `receipt_sha256` / `checksum`: Mini executor 生成的回执自身指纹。
- `payload_linked`: 已带任务包指纹的回执数量。
- `signed`: 已带有效回执自身指纹的回执数量。

旧回执没有这些字段时会显示为 `unsigned`，但不等于执行失败或回执被篡改。

Mini executor 推送服务器时也会在写入前验证 `receipt_sha256`；校验失败会拒写。历史无签名回执仍兼容读取，并在运维报告中归类为 `unsigned`。

### 已复盘失败归档

历史失败复盘完成后，可用以下命令从 active 队列归档到 reviewed 区：

```bash
PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/archive_reviewed_signals.py --apply --batch-id <id> --reason <reason>
```

归档会保留 manifest，支持按 `target_path -> source_path` 回滚。active `pending/claimed/running` 非空时工具会拒绝执行。
