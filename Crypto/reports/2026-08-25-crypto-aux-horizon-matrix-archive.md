# Crypto 辅助 horizon 矩阵逐表归档：四候选族 32 配置入已核验计数（research_only）

- 制定日期：2026-08-25。性质：计数归档文档，把 08-16 辅助 horizon 重测报告
  （`2026-08-16-crypto-ten-symbol-factor-prescreen-aux-horizons.md`）的全部
  唯一配置按「唯一配置去重后计数」口径正式登记；非新研究、无新读数。与
  同日 carry 双矩阵归档（`2026-08-25-crypto-carry-matrix-archive.md`，+32）
  同型互补；该文档预留的「矩阵式呈现暂未入计数」清单至此全部展开完毕。

## 归档枚举（32 配置）

| 候选族 | 网格 | 配置数 | 计数去向 |
|---|---|---|---|
| XS-RS 横截面相对强弱（long top-k 永远在场） | horizon {1h,4h,12h,24h} × variant {top_1,top_2,top_3} | 12 | 已核验 |
| 短期反转（per-symbol 超跌企稳） | horizon ×4 × variant {naive,strict} | 8 | 已核验 |
| Amihud 非流动性（long top-2） | horizon ×4 × variant {top_2} | 4 | 已核验 |
| 波动率 regime 修饰（momentum 高/低波动两半） | horizon ×4 × variant {high_vol_half,low_vol_half} | 8 | 已核验 |

去重声明：源报告的 1h 列是同一 180 天+ 快照上的重跑基线，与已计入
「十币A类 9」的 PR #291 九天窗读数窗口不同、逐行数值不同，按唯一配置
分别计数；四族信号定义互不重合；与横截面预登记网格 / 动量入场事件研究的
研究方法与数据窗均不同源。

## 结果事实（照录源报告）

- **四族在全部四个 horizon 上否决**：XS-RS / 反转 / Amihud 费用前 gross
  即全负——拉长 horizon 后没有可被成本摊薄显露的毛 edge；hit_rate 随
  horizon 从 ~24% 升至 ~44% 属标签机制而非预测力。
- 全样本与非重叠口径符号一致为负（除下条），排除重叠标签虚增假象。
- 唯二非重叠正单元（low_vol_half 12h +0.152% / 24h +0.168%）已被源报告
  逐条排除为小样本噪声：同变体全样本深负（差 ≈0.45pp）、符号随 horizon
  翻转、t≈1.6/1.0 远低于门槛、对 stride 相位敏感。

## 冻结口径下的判定

- **新增正收益因子：0。** 源报告结论维持：OHLCV-only、1h 特征族在
  1h→24h 全频段无 gross edge，「拉长 horizon 摊薄成本」路线证伪。
- **加密线已核验计数**：92+ → **124+**（+32，全部为已考核 concluded 身份；
  与既有计数同属无 PIT 回填读数、描述性身份，只有 receipt-bound 滚动评估
  可做判定）。计数只增不减。
- 源报告的滚动观察指引不变：low_vol_half 单元若在证据链 aux horizon 自然
  滚动结算中持续为正且样本翻倍仍存活才值得重新讨论——那是前瞻路径，
  不改变本归档的否决判定。

## 时点声明

本文档只做既有读数的计数登记；加密轨计数表（60+→92+→124+）的更新在
本文合入主线后的下一版记分板修订中执行（引用本文件为准）。research_only /
not_promotion_evidence；无代码、无数据面变更。
