# tradingagent/PM

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
预测市场交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- 本仓库 `PM/` 是现役预测市场交易研究、模拟盘和工具入口。
- 旧 `/opt/investment/PredictionMarkets/tools/` 只作历史迁移线索，不是生产代码入口；不得恢复为独立采集或执行依赖。
- CLOB sandbox + Dune Analytics
- shadow/sim/strategy 完整

## 特点
- 24/7, Polymarket
- 概率交易, 非方向性

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/PM/`。
- 数据入口: SharedSignals API/read model 只提供 PM 市场/价格；CLOB sandbox 仅作模拟/研究适配，不替代 SharedSignals 供数层。
- 概率判断入口: `PM/probability_model.py`。优先读取 `TRADINGAGENT_PM_MODEL_PROBABILITY_FILE` / `PM_MODEL_PROBABILITY_FILE` 或默认 `shared/review/pm/model_probabilities.jsonl` 的研究概率；缺研究概率时只使用 `pm_market_consensus_baseline` 标记“模型概率=市场概率、无独立 edge”，不得把判断概率写回 SharedSignals。
- 研究概率生成入口: `PM/research_probability.py` 与 wrapper `shared/wrappers/job_pm_research_probability.sh`。该任务通过 MarketGraph 统一 API `GET /pm/research-probabilities` / MCP `read_pm_research_probabilities` 读取独立研究概率，再与 SharedSignals PM 市场价格合并输出 `shared/review/pm/model_probabilities.jsonl`；无 MarketGraph 研究概率时会原子清空该文件，避免旧 edge 残留。
- SharedSignals PM 行内即使出现 `research_probability`、`marketgraph_probability` 或类似字段，也只能视为上游脏字段并忽略；TradingAgent 不用 SharedSignals 弱证据、情绪、类别、清晰度、流动性或到期时间生成 PM 独立概率。
- PM edge 计算必须同时具备 MarketGraph 研究概率和 SharedSignals 市场价；缺 SharedSignals 市场价时不得从 MarketGraph 研究行里的 `price` / `market_probability` 兜底。
- 历史迁移线索: `/opt/investment/PredictionMarkets/tools/`；如服务器仍有残留，默认按退役资产处理。
- 关键工具:
  - pm_workflow.py — Unified PredictionMarkets simulated-training workflow
  - pm_market_data.py — Read-only Polymarket market-data collection
  - pm_clob_sandbox.py — Polymarket CLOB trading sandbox (research only)
  - pm_simulator.py — YES/NO bets, settlement, early exit
  - pm_shadow_runner.py — Edge × kelly × hold variant comparison
  - report.py — Daily Brier + P&L shadow report
  - validation.py — Forward validation with calibration and Brier tracking
  - promotion.py — Strategy promotion scorecard (research→shadow→sim)
  - probability_model.py — Research probability intake + market-consensus baseline, never a data collector
  - research_probability.py — Conservative independent-probability generator into shared/review/pm
  - pm_prediction_model.py — Probability estimates with alpha
  - pm_marketgraph_bridge.py — PredictionMarkets → MarketGraph causal impact bridge
  - pm_historical_replay.py — Calibrate strategy on resolved markets
