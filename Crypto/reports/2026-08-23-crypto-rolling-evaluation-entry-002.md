# Crypto 首个滚动评估入口（MVP-2 receipt-bound）

- 契约：`tradingagent.crypto.forty_symbol_rolling_evaluation.v1`；shadow-only，authority=none。
- 输入为四十币观察器自身回执：append-only 成功事件（校验链完整）+ 逐槽不可变 K 线边车，逐行复算 identity/market-data 双哈希全部通过。
- 段：2026-08-23T05:55:00.000Z → 2026-08-23T07:15:00.000Z（每标的 17 根 5m，5 个连续回执槽，无缺口）。
- 冻结冠军单配置（阈值 0.001，3/12 根回看），零扫描；费用 taker 双边 0.1% + 每腿 2bps 滑点。

## 结果

| 指标 | 值 |
|---|---|
| 已了结回合 | 0 |
| 未了结（data_end，计为弃权）| 39 |
| 命中数 / 命中率 | 0 / None |
| 平均毛收益（已了结）| None |
| 平均净收益（已了结）| None |
| 最大回撤（净）| 0 |
| 买入持有基线平均净 | 0.01158568109959910251457292680 |

## 建议（机械规则，shadow-only）

`continue_accumulation` — resolved trips below the pre-declared minimum sample; keep accumulating rolling entries before any retain/downweight/disable judgement

本条目按序进入 MVP-2 滚动评估累积；负结果与弃权照实保留，不构成任何晋级证据，也不触发任何资本或风险行为。
