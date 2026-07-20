# tradingagent/HK

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
港股交易能力预留。

## 现状
- HK 代码、styles、wrapper 和本地工具已存在，但按 Nicholas 最新决策暂不接入生产模拟盘。
- `HK/styles/*.json` 保留为预留配置；生产默认不调度 HK，也不纳入默认 simulated health / evolution 范围。
- `shared/wrappers/job_hk_sim.sh` 和 `shared/wrappers/run_sim.py` 对 HK 默认 fail-closed；只有显式设置 `TRADINGAGENT_HK_SIM_ENABLED=1` 才可手动运行。
- TradingDatas fresh handoff 前不宣称任何 HK dataset 可用，也不臆造 `dataset_id`。若未来接入，只消费 `GET /v1/catalog` 与 `POST /v1/query`；现有 `hk_basic` / `hk_daily` 名称仅是旧 SharedSignals 数据线索，不是当前 dataset 合同或模拟交易输入。
