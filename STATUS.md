# TradingAgent 状态

> **给所有 agent：** 读完 [AGENTS.md](AGENTS.md) 理解规则后，读本文件理解"现在在哪、要去哪、能做什么"。
>
> **⚠️ 变更后必须更新本文件。**
>
> 最后更新：2026-07-02

---

## 一、当前状态

- **A 股影子盘**：完整闭环运行（信号生成 → 影子账簿 → 复盘）
- **A 股模拟盘**：通过 Mac Mini Hermes 执行，收/发/回执链路已修复
- **执行桥**：Mac Mini `~/.hermes/` 下 Hermes 正常运行，只执行和回写，不做买卖判断
- **PM（预测市场）**：影子盘每 10 分钟扫描运行
- **多市场**：PM/Crypto/US 共 61 个死 symlink 已清除，各市场 tools/ 目录仅保留 manifest.csv 索引，待独立实现
- **复盘节奏**：11:45 午盘 / 15:30 收盘 / 22:00 夜间校准 / 07:30 晨报
- **服务端**：杭州 `8.138.181.177`，生产路径 `/opt/investment/tradingagent/`
- **运行监控**：每小时运维报告（`ops_report.py`），覆盖执行队列、影子队列、回执完整性、PnL 摘要

## 二、已知问题

- Crypto/US/HK/PM 模拟盘链路未闭环（仅 A 股链路完整）
- 多市场代码依赖旧系统 symlink，已全部清除（61 个死 symlink），待独立实现
- 集合竞价支持标记为 STUB，未实现
- A 股实盘路径仍是人工（模拟盘信号 → 邮件发用户 → 手动执行）

## 三、下一步

1. [ ] Crypto/US/HK/PM 多市场工具独立实现（manifest.csv 中列出的 61 个工具从头构建）
2. [ ] 多市场模拟盘闭环（各自独立，不再依赖旧系统）
3. [ ] A 股实盘路径设计（需先确认安全边界和人工确认环节）
4. [ ] 集合竞价支持

## 四、活跃任务

- 无

## 五、最近完成（2026-07-02 Goal 1）

- [x] 旧系统残留清理：删除 61 个死 symlink（PM 20 + Crypto 21 + US 20）
- [x] 代码层 Tradings/KimiWork 引用全部修复（0 残留）
- [x] 服务器 crontab 37 条旧注释清理
- [x] 所有修改提交并 push，服务器同步确认

## 六、7/1 事故复盘（已完成）

以下事项已修复并固化为 [AGENTS.md](AGENTS.md) 中的永久规则：

- 虚假成交确认 → Mini/Hermes 健康门 + 未确认回执 halt
- 过期 pending 清理 → job_self_heal
- 回执指纹闭环 → receipt_sha256 验证
- 影子信号过滤 → 200xxx.SZ 等非普通 A 股代码三层过滤

详细时间线：[docs/runtime_incidents_20260701.md](docs/runtime_incidents_20260701.md)

## 六、关联系统状态

- [SharedSignals STATUS](../SharedSignals/STATUS.md) — 数据采集与存储状态
- [MarketGraph STATUS](../MarketGraph/STATUS.md) — 研究图谱与因果状态
- [Finance STATUS](../STATUS.md) — 根工作区总览
