# earnings 两臂 × 既有冻结标签族覆盖探针（research_only）

- 日期：2026-08-26。性质：C14 体例纯标签计数探针（#585 同款先例）；
  零收益计算、零新桶定义、不占面板编号（候选台账修订版 #560 仍
  OPEN）。目的：8·31 原始臂判定落地后，若任一臂 KEEP，「漂移有无条
  件层可做」是下一自然问题；本探针先行摸清既有基础设施的附着成本。
- 数据：彩排导出 labeled_outcomes（pos 165 / neg 206，ID 天然唯一零剔
  除）；身份自 ID 段提取（symbol=[2]，event_date 与入场日一致）；标签
  全部来自各研究模块既有冻结批量助手；缺档如实计为未标注。

## 覆盖表（计数层）

| 家族 | pos 覆盖 | pos 主要分布 | neg 覆盖 | neg 主要分布 |
|---|---|---|---|---|
| valuation | 100% | mid40/high36/loss36/low36/sh17 | 100% | **loss125**/mid44/high24/low13 |
| turnover | 100% | normal154/surge7/shrink4 | 100% | normal181/shrink21/surge4 |
| macro 发布窗 | 78.2% | outside103/**ante13**/sd8/post5 | 84.0% | outside143/**ante14**/sd13/post3 |
| chips | 100% | mid65/profit56/underwater44 | 99.5% | **underwater82**/mid77/profit46 |
| pledge | 100% | no_snap85/mid40/low29/high11 | 100% | no_snap99/mid56/low31/high20 |
| holdernum | 100% | **expand65**/stable52/contract47 | 100% | **contract81**/stable79/expand44 |
| topinst | 100% | no_listing142/netbuy12/netsell6 | 100% | no_listing176/nb8/ns7 |
| toplist | 100% | no_listing142/rise14/sell8 | 100% | no_listing176/sell21/rise9 |
| blocktrade | 100% | none157/flat6/deep2 | 100% | none201/flat4/deep1 |
| absorption | 100% | balanced128/outflow24/inflow13 | 100% | balanced149/outflow41/inflow16 |
| repurchase | 100% | no_rec158/active5/done2 | 100% | no_rec200/active3/done3 |
| holdertrade | 100% | no_rec161/nsell3/nbuy1 | 100% | no_rec199/nsell6/nbuy1 |

结构性排除（未计算）：holdertype 批次构成与 unlock supply 为解禁批次
专属语义，对 earnings 无对象。无现成批量助手需适配工作：dividend 姿态
（私有单点函数）、价格结构 r63（逐条接口）、margin 自融。

## 计数层结构观察（非读数、不作判定输入）

1. **neg 臂估值缺失高度集中**（loss_or_missing 125/206=61%，pos 仅
   22%）——负向预告不成比例地来自亏损股（pe_ttm 无定义），经济上自洽；
   neg 臂未来任何估值条件化必须把 loss_or_missing 当独立桶而非缺档剔除，
   否则会静默丢掉六成样本。
2. **holdernum 轴两臂变化真实且方向相反**（pos 以 expand 为主 65 vs
   neg 以 contract 为主 81）——股东户数轴是未来条件化候选中变异最健
   康的一条。
3. macro ante 两臂各有 13/14 例——观察名单项 rule[ante] 未来可在
   earnings 臂上做跨信号对照（独立预注册前提）。
4. 事件窗活动类家族普遍稀疏（repurchase active ≤5、holdertrade ≤6、
   blocktrade deep ≤2）——当前 n≈200 下不具备条件化门柱资格。

## 对判定后决策的意义（预置，不预支）

若周一某臂 KEEP：条件化立项优先序建议按「覆盖率 × 桶间变异 × 细胞
规模」排——valuation / holdernum / chips / macro 四族具备可行性；
turnover 的 surge/shrink 与 absorption 的 inflow/outflow 属薄桶观察
级。若双臂 FAIL：本表作废归档，无后续动作。所有条件化都必须走独立
预注册→引擎→读数标准链路，本文档不构成任何假设绑定。

## 边界声明

纯标签计数；先于任何 earnings 分桶收益计算合入主线；research_only /
not_promotion_evidence；不移动门柱、不产生部署候选。
