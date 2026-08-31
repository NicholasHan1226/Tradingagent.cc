# Crypto Cost and Risk Review

## Executive Summary

2026-08-30 的 G5 模拟账本仍亏损 **858.52 USDT（−8.59%）**。成本归因、历史风控复算、
提案分类与可重跑研究入口已完成本地验证；没有形成可晋级的盈利策略，也没有生产自动接入。

本报告为候选交接记录。可视化入口是同目录的
[规范报告 artifact](2026-08-30-cost-risk-review.artifact.json)，已通过 Data Analytics
artifact 验证并由原生 report renderer 接受；尚无独立像素截图验收。无 Sites 公开发布。

## 新鲜模拟账本证据

[完整归因与 health](2026-08-30-paper-loss-attribution.json)：
2026-08-30 06:28:35.265181Z 只读提取，账本有效估值 06:21:00Z（北京时间 14:21）。
source release 为 `bb4413864ef0b76452916e87e48fdc72e69a7deb`，capital head 11685，
checksum `c9ad7a1b41a5569afc9aa3e6d934a0fba3b4a2ea199d3bdfbc040c2b9d1cc12d`。
当前时间不能由此快照外推；全部成交为 fixture_simulated，不是交易所账户。

| 损益项 | USDT |
| --- | ---: |
| 期初现金 | 10000 |
| 期末权益 / 现金 | 9141.48018936 |
| 报价中点价格变动毛贡献 | +307.17373320 |
| 模型买卖价差成本 | −90.56328860 |
| 模型滑点与舍入成本 | −180.18563080 |
| 手续费 | −894.94462444 |
| 净损益 | −858.51981064 |

四项损益严格相加等于净损益，六项账本 reconciliation residual 均为零。
896 条成交腿，448 次完整往返；本快照无持仓。BTC 净损益 −443.58246891，
ETH −414.93734173。每次往返平均价格毛贡献约 0.69，模型执行成本加手续费约 2.60 USDT。
这证明现有成交的成本负担，不能证明提高信号门槛后仍保留同样毛收益。

归因使用 bid/ask 中点而不是已含模型成本的 order.reference_price；partial sell
成本与买入手续费分摊遵循现有账本舍入规则。只读入口持有既有共享锁并验证 checkpoint
及链头，不创建锁/账户、重建 checkpoint、改写交易或刷新标记价。

## 固定参数历史风险实验

[完整结果、逐笔账本与 TD receipts](2026-08-30-risk-replay.json)：
catalog `v1-c3011487473156b0`，50 页 receipt 记录，仅正式 catalog/query 输入。
窗口 2026-04-02T00:00Z 至 2026-08-28T00:00Z exclusive，共 148 天。
报告哈希 `501ed63030f6891fe7cfa826fa29a07e76bbc582cde3960e53429a2ff27e3e76`。

| 研究对照 | 净收益 | 日收盘最大回撤 | 成交腿数 |
| --- | ---: | ---: | ---: |
| 原趋势 | +3.60% | 15.45% | 66 |
| 日采样风控趋势 | −0.77% | 7.56% | 48 |
| 事前敞口 BTC＋现金 | +3.99% | 15.85% | 17 |
| 全仓 BTC | +17.52% | 28.69% | 2 |
| 现金 | 0% | 0% | 0 |

原冻结信号未改。风险常量：日亏损 3%、连续亏损 3 批、回撤 5% 时风险预算乘 0.75、
回撤 7% 时暂停；研究版暂停持续有效、不自动恢复。收盘触发等待下一已知 00:05 开盘，
卖出后和每次买入后重新检查风险。2026-05-20 平仓后持续持有现金，不能把结果描述成
整段期间仍有策略参与。原趋势未跑赢该 BTC＋现金对照；它只是事前总敞口对照，
不是事后严格波动率匹配，也不能据此宣称 alpha。

日采样并非完整 5 分钟风控路径，采样最大回撤 7.5837%，故 7% 不是最大损失保证。
输入为历史回填，没有 contemporaneous PIT 证据；成交是采样价假设，不含盘口深度及交易所
过滤器。本轮不修改原前向窗口、K10 或参数，不基于已见结果继续搜索。

主助手将同一 TD 输入复制到本地，独立重跑 `slow_trend_risk_replay.analyze`：
完整 JSON 与 source/报告哈希逐项一致；另从逐笔数量、价格、费用重建现金与持仓，
两种新对照全部对账通过，终态仓位为零，ledger_sha256 一致。

## 提案到重评的真实副本验收

[两次执行 receipt 和源文件指纹](2026-08-30-research-cycle-acceptance.json)；
[重评指标摘录](2026-08-30-research-cycle-metrics.json)。

源是 TA 自有 observation 文件的隔离本地副本，不是消费者直读 TD 数据库。源文件 6176 个，
摘要 `535fdceb6e5c4e869f20581cb2ae6f846177c562f135d7a1cb399fc5f16b1c71`；
两次执行前后不变，第二次生成的研究产物字节一致。2065 个终态窗口，2048 有效、
17 无效，最新有效 slot 2026-08-30T06:20Z。

- 新 `--include-proposals` 显式入口：生成提案 → 可用性分类 → 原四候选重评。
- generator v3 使用独立命名空间；旧 v1/v2 产物不读、不迁移、不覆写。
- 23 个提案都因所需 B 数据面不完整而 blocked；注册/评估字段均不虚报。
- 原四候选重评 32 cells；正向单元仍只记 positive_in_sample_only，不能晋级。
- 当前副本不足 60 天慢趋势 lookback；不把外部历史 API 结果伪装成 observation 自然积累。
- 第二次重评返回 no_new_input；中断可重跑已完成阶段，完整性错误仍失败退出，
  不删状态、不自动修复损坏账本、不调用 strategy evaluation/资本写入。
- 当前生产 worker 没有接入此新入口；本地幂等不等于定时自动运行或策略自动优化已上线。

TA 任务另行报告 40-symbol observer 在 14:29/14:34 的新周期仍有 budget_deferred /
watermark_invalid；可选 spread 耗尽预算丢失尚未保存 bars 的情形已有独立内存复现，
但线上根因尚未确认。该问题由 TA 的 #607/P0 队列处理，本批不修改 observer。
上述 2048 窗口是 ten-symbol 历史副本，不是 40-symbol 当前消费成功的证据。

## TD 套息数据交接

独立核查入口：[TradingDatas 草稿 #395](https://github.com/NicholasHan1226/TradingDatas/pull/395)，
head `65df6af79b7e1806afa2cc7645dc750d0a344a86`；本轮 gh 读回确认仅一份日期化交接文档。

现有 BTC/ETH funding 事件未保存 settlement mark，spot 5m 与 premium-index 不能替代
perp trade/mark 5m。历史 effective-dated funding schedule 未有正式来源，保持 data_unavailable。
添加 mark 会改变 append-only payload 身份；须由 TD Data-Contract Owner + Datas PM 接受
新 schema major、审阅的迁移/dual-read 或另行批准的新合同之一。尚未选择，也没有实施 TD
provider/registry/数据库/生产变更。该阻塞不影响本批已有数据研究成果。

## 范围、验证和回滚

候选分支 `codex/crypto-risk-attribution-loop`，stacked base 为
`98499b961d5373111d6f5802c318148dd3e4e5e5`（PR #606 head）。
#606 保持 draft 且内容不变；本候选不做主线/发布验收承诺。当前 TA release/helper/systemd
仍由 TA 任务独占，本任务无该写域。未恢复旧 Controller 或写 AUTODEV_STATE。

代码范围：
`Crypto/paper_loss_attribution.py`、
`Crypto/slow_trend_risk_replay.py`、
`Crypto/ten_symbol_hypothesis_generator.py`、
`Crypto/ten_symbol_research_loop.py`，对应四份测试；
`Crypto/README.md`、`Crypto/AGENTS.md` 和本批日期化证据。
文档已同步入口、真实能力边界及回滚。独立协作者的新会话完整读取规则，通过规则发现 smoke，
只读审计未发现阻断项；主助手另行复算和测试，不以协作者口头状态替代证据。

主助手实际运行（173 passed，85.58s）：

```sh
REAL_TRADING_ENABLED=false PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q -p no:cacheprovider \
  tests/test_crypto_paper_loss_attribution.py \
  tests/test_crypto_slow_trend_risk_replay.py \
  tests/test_crypto_slow_trend_research.py \
  tests/test_crypto_research_accounting.py \
  tests/test_crypto_ten_symbol_hypothesis_generator.py \
  tests/test_crypto_ten_symbol_research_loop.py \
  tests/test_crypto_ten_symbol_factor_research_worker.py \
  tests/test_crypto_ten_symbol_factor_strategy_evaluation.py \
  tests/test_crypto_delayed_paper_round_trip_health.py
```

最终完整 patch 的 `git diff --check` 通过。新模块复测 36 passed（1.97s）；
报告 JSON 解析、实际执行的可视化 SQL 与 snapshot 一致性检查通过。
原生 report manifest/snapshot 验证与渲染调用成功。
未运行整个仓库回归、生产新入口自然周期、真实交易执行、逐 5 分钟风险路径验收；
候选 CI 与最终合入验收分开核对。恢复方式是停止调用新研究入口并切回前一候选代码，
保留所有旧/新研究产物；没有生产切换需要回滚，也不删除账本或历史 evidence。

## 下一步

在新的预先声明研究批次中检验换手筛选和更细风险路径；不以杠杆放大未验证信号。
等待精确研究候选审核与 TD 身份迁移决策。没有盈利保证，也没有晋级或资本权限。
