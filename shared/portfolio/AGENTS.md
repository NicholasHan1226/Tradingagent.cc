# portfolio/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## V1 当前入口

- `champion.py` — 冻结、可复现的未校准排序 Champion；输出绑定当前人工选择 manifest、symbol、PIT 决策时点、数据 receipt/vintage/lineage、完整特征快照和精确 Champion spec 的内容寻址 score receipt，不声称概率。
- `small_account_optimizer.py` — 5万元模拟账户的独立账户proof、买入整手/卖出零股例外、双边费用、现金、T+1、单票/总仓及固定最小经济 probe 订单优化。
- V1 只消费确定性研究分、账户/持仓权威与版本化资本政策；LLM 证据不参与权重计算。
- V1 目标仓位统一进入 `shared/runtime/` 的风险门禁和模拟执行；本目录不能自行连接 broker 或扩张风险。

## V1 原则

- 买入100股整数倍；卖出只允许100股整数倍、完整零股余额或全部退出且受T+1约束。组合容量、单票15%、总敞口90%等参数以版本化A股资本政策为准，禁止复制漂移常量。
- 现金是正式候选动作；没有合格净边际时允许不交易。
- 未校准 rank 只允许确定候选先后顺序，不得乘入目标金额或仓位。Phase 1 新仓使用与 rank 无关的固定最小经济 probe；这只是工程模拟 sizing，不是统计最优仓位、收益概率或 Kelly 权限。
- 目标权重变化未覆盖完整费用、不确定性缓冲或 no-trade band 时不产生订单。
- 每个计划必须绑定版本化 `cost_policy_id`；day loop 独立复算佣金、过户费和卖出印花税，不能信任调用方自报或重新签名的 fee。
- 已有持仓、mark、现金/gross、当日买入锁定和卖出可用量必须来自完整账户快照，并由无默认`AccountAuthorityVerifier`生成detached proof；不得由候选或调用方自行声明。fixture proof永远不可晋级。
- 主板范围在优化器入口和最终执行入口双重校验；双创个股不得进入持仓计划。
- Champion 只接受显式 `tradingagent.numeric_pit_features.v1` namespace 中登记的数值 PIT 特征；来源、vintage、lineage 与数据 receipt 必须随 score receipt 绑定。不得依赖字段名关键词黑名单推断来源安全。
- LLM 仅可在决策说明中附加 evidence reference，不得进入 Champion 特征 namespace、rank、仓位、风险放宽、目标持仓或订单字段。

## 旧模块与退役

- `constructor.py`、`position_sizer.py`、`rebalancer.py` 是旧组合兼容链，仍含 `conviction_weighted`/`belief_score` 等历史语义；旧多市场 `exit_manager.py` 已物理退役。
- 这些模块不得被 V1 `champion → small_account_optimizer → runtime` 链导入，也不得新增 A 股消费者。
- 精确路径、剩余消费者、删除前提和回滚规则登记在 `shared/governance/legacy_inventory.yaml`；兼容完成后按 Phase 3 清理，不长期双轨。

## 验收

- V1 数据流静态门禁证明未导入旧 constructor/position_sizer/adversarial sizing。
- 组合测试覆盖已有持仓、买卖双边费用、整数股数、T+1、候选超过容量、主板范围及幂等恢复。
- 原始调用方 `rank_score`、伪造/篡改 score receipt、非当前 Champion selection、或 fixture rank 冒充 canonical predictive authority 都应在决策前 fail closed。
- 任何 LLM/旧 belief 字段出现在 V1 Champion 特征 namespace、仓位或订单链都应使验收失败。
